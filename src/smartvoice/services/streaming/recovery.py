"""Bounded in-process recovery; transport reconnects never recreate ASR state."""
import hashlib
import json
import secrets
from collections import OrderedDict
from smartvoice.domain.streaming import StageError


class RecoveryState:
    max_events = 128
    max_bytes = 8 * 1024 * 1024
    input_history = 128

    def __init__(self, session, grace_seconds):
        self.session = session
        self.token = secrets.token_urlsafe(32)
        self.grace_seconds = grace_seconds
        self.attached = True
        self.expiry_task = None
        self.events = OrderedDict()
        self.bytes = 0
        self.frames = OrderedDict()

    def ready(self):
        return dict(resume_token=self.token, grace_seconds=self.grace_seconds,
                    input_history_frames=self.input_history, max_replay_bytes=self.max_bytes)

    def remember(self, event):
        payload=event.get('audio',b'')
        metadata={k:v for k,v in event.items() if k!='audio'}
        if payload:metadata['audio_bytes']=len(payload)
        size=len(payload)+len(json.dumps(metadata,ensure_ascii=False).encode())
        if event['type'] not in ('error', 'session_complete') and (
                len(self.events) >= self.max_events or self.bytes + size > self.max_bytes):
            raise StageError('resume_buffer_overload', 'Bounded recovery output cache is full')
        self.events[event['event_sequence']] = (event, size)
        self.bytes += size

    def prune(self):
        session = self.session
        for sequence, (event, size) in list(self.events.items()):
            if event['type'] in ('error', 'session_complete'): continue
            audio = event.get('audio_sequence')
            unplayed = (audio is not None and session.plan.output_consumption == 'playback'
                        and audio > session.speech.last_played)
            if sequence <= session.last_ack and not unplayed:
                del self.events[sequence]; self.bytes -= size

    async def push(self, sequence, start_sample, pcm):
        session = self.session
        if type(sequence) is not int or type(start_sample) is not int or sequence < 1 or start_sample < 0:
            raise StageError('invalid_audio_sequence', 'Expected positive frame sequence and nonnegative sample offset')
        fingerprint = (start_sample, len(pcm), hashlib.sha256(pcm).digest())
        if sequence <= session.input_sequence:
            if self.frames.get(sequence) != fingerprint:
                raise StageError('audio_replay_conflict', 'Repeated audio differs or is outside the deduplication window')
            return self.input_ack()
        if sequence != session.input_sequence+1 or start_sample != session.samples:
            raise StageError('audio_gap', 'Audio must continue at the accepted sequence and sample offset')
        await session.push_audio(pcm)
        self.frames[sequence] = fingerprint
        while len(self.frames) > self.input_history: self.frames.popitem(last=False)
        return self.input_ack()

    def input_ack(self):
        return dict(type='input_ack', input_sequence=self.session.input_sequence,
                    accepted_samples=self.session.samples)

    def resume(self, last_event_sequence, last_audio_played):
        session = self.session
        if self.attached: raise StageError('resume_in_use', 'Session already has an attached transport')
        if (type(last_event_sequence) is not int or
                not session.last_ack <= last_event_sequence <= session.sequence):
            raise StageError('resume_cursor_invalid', 'Event cursor is stale or outside emitted output')
        if type(last_audio_played) is not int or last_audio_played < 0:
            raise StageError('resume_cursor_invalid', 'Invalid played audio cursor')
        speech = session.speech
        if not speech or session.plan.output_consumption != 'playback':
            if last_audio_played: raise StageError('resume_cursor_invalid', 'Played cursor requires playback consumption')
        else:
            if not speech.last_played <= last_audio_played <= speech.sequence:
                raise StageError('resume_cursor_invalid', 'Played cursor is stale or outside emitted audio')
            for sequence in range(speech.last_played+1, last_audio_played+1):
                row = speech.ledger.get(sequence)
                if row is None or row['event_sequence'] is None or row['event_sequence'] > last_event_sequence:
                    raise StageError('resume_cursor_invalid', 'Played audio must reference received output')
            for sequence in range(speech.last_played+1, last_audio_played+1):
                if sequence > speech.last_started: speech.started(sequence)
                speech.played(sequence)
            speech.last_started = speech.last_played
        session.acknowledge(last_event_sequence)
        self.prune()
        replay = [event for sequence, (event, _) in self.events.items()
                  if event['type'] in ('error','session_complete') or sequence > last_event_sequence or (event.get('audio_sequence', 0) > last_audio_played
                  and session.plan.output_consumption == 'playback')]
        self.attached = True
        session.heartbeat()
        response = dict(type='session_resumed', protocol='smartvoice.stream.v1', session_id=session.id,
                        plan=session.plan.public(), input_finished=session.input_finished,
                        last_event_sequence=session.last_ack, last_audio_played=speech.last_played if speech else 0,
                        context_version=session.context.version,
                        heartbeat_interval_seconds=max(.01, session.settings.streaming_idle_seconds/3),
                        **{k:v for k,v in self.input_ack().items() if k!='type'})
        return response, replay
