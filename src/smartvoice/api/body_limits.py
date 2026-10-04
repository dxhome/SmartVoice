"""Bound speech JSON bytes before framework parsing."""
from starlette.responses import JSONResponse

class SpeechBodyLimit:
    """Bound bytes before the framework buffers/parses the JSON body."""
    def __init__(self,app,maximum): self.app,self.maximum=app,maximum

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method')!='POST' or scope.get('path')!='/v1/audio/speech':
            return await self.app(scope,receive,send)
        chunks=[];total=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect': return
            chunk=message.get('body',b'');total+=len(chunk)
            if total>self.maximum:
                response=JSONResponse({'error':{'code':'payload_too_large','message':'Speech JSON body exceeds the configured byte limit'}},status_code=413)
                return await response(scope,receive,send)
            chunks.append(chunk)
            if not message.get('more_body',False):break
        first=True
        async def replay():
            nonlocal first
            if first:
                first=False
                return {'type':'http.request','body':b''.join(chunks),'more_body':False}
            return await receive()
        await self.app(scope,replay,send)

