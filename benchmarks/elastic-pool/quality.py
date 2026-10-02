"""Compare saved concurrent TTS samples with fresh serial synthesis via STT."""
import json
import re
from pathlib import Path

from smartvoice.adapters.inference.factory import create_inference_provider
from smartvoice.config.settings import Settings, default_data_dir

ROOT = Path('.smartvoice-dev/sandbox/sherpa-production-20261002')
CONFIG = json.loads((Path(__file__).parent / 'scenarios.json').read_text())['scenarios']
TEXTS = ['你好，欢迎使用本地语音服务。', '今天的天气很好，我们一起出去走走吧。',
         '语音识别和语音合成可以在本地完成。', '这是一次并发性能测试，请稍等片刻。']


def normalize(text):
    return re.sub(r'[^\w]', '', text.lower())


def main():
    provider = create_inference_provider(Settings(data_dir=default_data_dir(), max_instances=1))
    results = {}
    try:
        for scenario in ('tts', 'supertonic'):
            spec = CONFIG[scenario]
            texts = spec.get('texts', TEXTS)
            rows = []
            for i, text in enumerate(texts):
                concurrent_audio = (ROOT / f'{scenario}-sample-{i}.wav').read_bytes()
                serial = [provider.synthesize(text, model_id=spec['model_id'], language=spec['language']).audio
                          for _ in range(2)]
                decode = lambda audio: provider.transcribe(audio, spec['language'], 'stt-sensevoice-small-int8')['text']
                concurrent_text = decode(concurrent_audio)
                serial_texts = [decode(audio) for audio in serial]
                rows.append({'input': text, 'concurrent_stt': concurrent_text, 'serial_stt': serial_texts,
                             'matches_input': normalize(concurrent_text) == normalize(text),
                             'matches_serial': any(normalize(concurrent_text) == normalize(t) for t in serial_texts)})
            results[scenario] = rows
    finally:
        provider.close()
    (ROOT / 'quality.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
