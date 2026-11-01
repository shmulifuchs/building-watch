import base64
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


class ProviderError(Exception):
    def __init__(self, outcome, message):
        self.outcome = outcome
        super().__init__(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Whapi:
    def __init__(self, credentials):
        self.token = json.loads(Path(credentials).read_text())['token']
        if not isinstance(self.token,str) or not self.token.strip():
            raise ValueError('Missing Whapi token')
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, payload=None):
        headers = {'Authorization':'Bearer '+self.token,'Accept':'application/json',
                   'User-Agent':'BuildingWatch/2','Content-Type':'application/json'}
        req = urllib.request.Request('https://gate.whapi.cloud'+path,
                                     data=None if payload is None else json.dumps(payload).encode(),headers=headers)
        try:
            with self.opener.open(req,timeout=20) as response:
                result = json.load(response)
                if not isinstance(result,dict):
                    raise ValueError('Expected a JSON object')
                return result
        except urllib.error.HTTPError as error:
            outcome = 'retryable' if error.code == 429 else ('unknown' if error.code >= 500 else 'permanent')
            raise ProviderError(outcome,'HTTP '+str(error.code)) from None
        except urllib.error.URLError as error:
            outcome = 'retryable' if isinstance(error.reason,(socket.gaierror,ConnectionRefusedError)) else 'unknown'
            raise ProviderError(outcome,'Connection failed') from None
        except (TimeoutError,OSError,ValueError):
            raise ProviderError('unknown','Response unavailable or invalid') from None

    def send(self, body):
        if 'media_path' in body:
            path = Path(body['media_path'])
            if not path.is_file() or not 0 < path.stat().st_size <= 90_000_000:
                raise ProviderError('permanent','Video missing or larger than 90 MB; split/re-encode before attaching')
            media = 'data:video/mp4;name=clip.mp4;base64,'+base64.b64encode(path.read_bytes()).decode()
            return self.request('/messages/video',dict(to=body['destination'],media=media,mime_type='video/mp4',caption=body['text']))
        return self.request('/messages/text',dict(to=body['destination'],body=body['text']))

    def status(self, message_id):
        result = self.request('/messages/'+quote(message_id,safe=''))
        message = result.get('message',result)
        if not isinstance(message,dict):
            raise ProviderError('unknown','Invalid status response')
        return message


class Delivery:
    """Separate worker: retries known rejections, reconciles acceptance, holds ambiguous sends."""
    def __init__(self, store, provider, max_attempts=5):
        self.store,self.provider,self.max_attempts = store,provider,max_attempts
        store.db.execute("UPDATE bw_queue SET status='delivery_unknown',error='worker_restart_during_send' WHERE status='sending'")

    def run_once(self, now):
        rows = self.store.db.execute("SELECT * FROM bw_queue WHERE status='pending' AND due<=? ORDER BY id LIMIT 10",(now,)).fetchall()
        for row in rows:
            with self.store.transaction():
                claimed = self.store.db.execute("UPDATE bw_queue SET status='sending',attempts=attempts+1 WHERE id=? AND status='pending'",(row['id'],)).rowcount
            if not claimed:
                continue
            try:
                result = self.provider.send(json.loads(row['body']))
                message = result.get('message')
                message_id = message.get('id') if isinstance(message,dict) else None
                if result.get('sent') is not True or not message_id:
                    raise ProviderError('unknown','Provider acceptance not confirmed')
                self.store.db.execute("UPDATE bw_queue SET status='accepted',provider_id=?,checked=?,due=?,error=NULL WHERE id=?",(message_id,now,now,row['id']))
            except ProviderError as error:
                attempts = row['attempts']+1
                status = ('pending' if error.outcome == 'retryable' and attempts < self.max_attempts
                          else 'delivery_unknown' if error.outcome == 'unknown' else 'failed')
                self.store.db.execute('UPDATE bw_queue SET status=?,due=?,error=? WHERE id=?',
                                      (status,now+min(3600,5*2**(attempts-1)),str(error),row['id']))
                self.store.audit(now,row['building'],'delivery_'+status,queue_id=row['id'],attempt=attempts,error=str(error))
        for row in self.store.db.execute("SELECT * FROM bw_queue WHERE status='accepted' AND checked<=? LIMIT 10",(now-60,)).fetchall():
            if now-row['due'] >= 86400:
                self.store.db.execute("UPDATE bw_queue SET status='delivery_unconfirmed',error='no_delivery_confirmation_within_24h' WHERE id=?",(row['id'],))
                self.store.audit(now,row['building'],'delivery_unconfirmed',queue_id=row['id'])
                continue
            self.store.db.execute('UPDATE bw_queue SET checked=? WHERE id=?',(now,row['id']))
            try:
                message = self.provider.status(row['provider_id'])
                body = json.loads(row['body'])
                if message.get('id') != row['provider_id'] or message.get('chat_id',body['destination']) != body['destination']:
                    raise ProviderError('permanent','Status does not match message and destination')
                status = message.get('status')
                if status in ('delivered','read','played'):
                    self.store.db.execute("UPDATE bw_queue SET status='delivered',error=NULL WHERE id=?",(row['id'],))
                elif status == 'failed':
                    self.store.db.execute('UPDATE bw_queue SET status=?,due=?,error=? WHERE id=?',
                                          ('pending' if row['attempts'] < self.max_attempts else 'failed',now+60,'provider_confirmed_failed',row['id']))
            except ProviderError as error:
                self.store.audit(now,row['building'],'delivery_status_check_failed',queue_id=row['id'],error=str(error))


def attach_media(store, job_id, path, media_root, now):
    path,root = Path(path).resolve(),Path(media_root).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() != '.mp4':
        raise ValueError('Attach an existing MP4 inside the configured media root')
    if not 0 < path.stat().st_size <= 90_000_000:
        raise ValueError('Split/re-encode recording into files of at most 90 MB')
    with store.transaction():
        job = store.db.execute('SELECT * FROM bw_media WHERE id=?',(job_id,)).fetchone()
        if not job or job['status'] != 'awaiting_recording':
            raise ValueError('Unknown job or job already attached')
        body = json.loads(job['body'])
        part = ' Part '+str(body['part'])+'.' if 'part' in body else ''
        caption = body['building_name']+': '+body['type'].replace('_',' ')+part+' '+body['start']+' to '+body['end']
        if body.get('recording_gap_possible'):
            caption += ' Camera coverage was interrupted; footage may be incomplete.'
        store.enqueue(job['building'],'video:'+job['key'],dict(**body,text=caption,media_path=str(path)),now)
        store.db.execute("UPDATE bw_media SET status='attached',path=? WHERE id=?",(str(path),job_id))
        store.audit(now,job['building'],'media_attached',job_id=job_id)
