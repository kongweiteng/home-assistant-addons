"""Closed private preview protocol. No user URL/CDP/host path contract."""
from dataclasses import dataclass, asdict
from datetime import datetime
import base64
import hashlib
import json
import re
import struct
import zlib

from .errors import WorkRuntimeError
from .work_execution import SHANGHAI, _ref

MAGIC=b'CWP1\n'; MAX_FRAME=6*1024**2; MAX_PNG=4*1024**2
MAX_SPOOL=96*1024**2; MAX_TOTAL=64*1024**2
KEYS=frozenset({'Tab','Enter','Escape','Backspace','Delete','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End'})

def fail(code, *, uncertain=False):
    return WorkRuntimeError(code,'Private preview policy rejected the operation.',uncertain=uncertain)

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()

def decode(raw):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError()
            result[key]=value
        return result
    try:
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if not isinstance(value,dict):raise ValueError()
        return value
    except (TypeError,ValueError,UnicodeError,RecursionError):raise fail('preview_protocol_invalid') from None

class PreviewFrames:
    def __init__(self):self.buffer=bytearray();self.started=False;self.total=0
    def feed(self,data):
        self.total+=len(data)
        if self.total>MAX_SPOOL:raise fail('preview_spool_limit')
        self.buffer.extend(data)
        if not self.started:
            if len(self.buffer)<len(MAGIC):return []
            if self.buffer[:len(MAGIC)]!=MAGIC:raise fail('preview_protocol_invalid')
            del self.buffer[:len(MAGIC)];self.started=True
        result=[]
        while len(self.buffer)>=4:
            size=struct.unpack('!I',self.buffer[:4])[0]
            if not 1<=size<=MAX_FRAME:raise fail('preview_frame_limit')
            if len(self.buffer)<size+4:break
            result.append(decode(bytes(self.buffer[4:size+4])));del self.buffer[:size+4]
        if len(self.buffer)>MAX_FRAME+4:raise fail('preview_frame_limit')
        return result
    def finish(self):
        if not self.started or self.buffer:raise fail('preview_protocol_incomplete')

@dataclass(frozen=True)
class PreviewLease:
    owner_ref:str
    task_ref:str
    task_revision:int
    input_revision:int
    lease_ref:str
    lease_epoch:int
    started_at:datetime
    expires_at:datetime
    def validate(self,now):
        for value in (self.owner_ref,self.task_ref,self.lease_ref):_ref(value)
        for value in (self.task_revision,self.input_revision,self.lease_epoch):
            if type(value) is not int or not 1<=value<=2**53-1:raise fail('preview_scope_invalid')
        if (not all(isinstance(v,datetime) and v.utcoffset() is not None for v in (now,self.started_at,self.expires_at))
                or not self.started_at<=now<self.expires_at or not 0<(self.expires_at-self.started_at).total_seconds()<=1800):
            raise fail('preview_lease_expired')
    def document(self):
        value=asdict(self)
        for key in ('started_at','expires_at'):value[key]=value[key].astimezone(SHANGHAI).isoformat()
        return value
    @property
    def digest(self):return 'sha256:'+hashlib.sha256(canonical(self.document())).hexdigest()

def event(value):
    if not isinstance(value,dict) or not isinstance(value.get('kind'),str):raise fail('preview_event_invalid')
    kind=value['kind'];size=0
    if kind=='click':
        valid=(set(value)=={'kind','x','y'} and type(value['x']) is int and 0<=value['x']<1024 and type(value['y']) is int and 0<=value['y']<768)
    elif kind=='text':
        valid=set(value)=={'kind','text'} and isinstance(value['text'],str) and 1<=len(value['text'])<=1024
        if valid:
            try:size=len(value['text'].encode('utf-8',errors='strict'))
            except UnicodeError:valid=False
            valid=valid and size<=4096 and not any((ord(c)<32 and c not in '\n\t') or ord(c)==127 for c in value['text'])
    elif kind=='key':valid=set(value)=={'kind','key'} and isinstance(value['key'],str) and value['key'] in KEYS
    elif kind=='scroll':valid=set(value)=={'kind','delta_y'} and type(value['delta_y']) is int and -720<=value['delta_y']<=720
    elif kind=='refresh':valid=set(value)=={'kind'}
    else:valid=False
    if not valid:raise fail('preview_event_invalid')
    return dict(value),size

def render_ack(value):
    if not isinstance(value,dict) or set(value)!={'state'} or value['state']!='render_pending':
        raise fail('preview_response_invalid')
    return {'state':'render_pending'}

def png(value):
    if (not isinstance(value,dict) or set(value)!={'mime','width','height','image_b64'}
            or value['mime']!='image/png' or type(value['width']) is not int or value['width']!=1024
            or type(value['height']) is not int or value['height']!=768
            or not isinstance(value['image_b64'],str) or len(value['image_b64'])>5592408):raise fail('preview_png_invalid')
    try:
        data=base64.b64decode(value['image_b64'],validate=True)
        if len(data)>MAX_PNG or base64.b64encode(data).decode()!=value['image_b64'] or data[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError()
        offset=8;stage=0;packed=bytearray();channels=0;count=0
        while offset<len(data):
            count+=1
            if count>256 or len(data)-offset<12:raise ValueError()
            size=struct.unpack('!I',data[offset:offset+4])[0];end=offset+12+size
            if end>len(data):raise ValueError()
            kind=data[offset+4:offset+8];body=data[offset+8:end-4]
            if zlib.crc32(kind+body)&0xffffffff!=struct.unpack('!I',data[end-4:end])[0]:raise ValueError()
            if kind==b'IHDR' and stage==0 and size==13:
                w,h,depth,color,compress,filtering,interlace=struct.unpack('!IIBBBBB',body)
                if (w,h,depth,compress,filtering,interlace)!=(1024,768,8,0,0,0) or color not in {2,6}:raise ValueError()
                channels=3 if color==2 else 4;stage=1
            elif kind==b'IDAT' and stage in {1,2}:packed.extend(body);stage=2
            elif kind==b'IEND' and stage==2 and size==0 and end==len(data):stage=3
            else:raise ValueError()
            offset=end
        if stage!=3:raise ValueError()
        maximum=(1024*channels+1)*768;decoder=zlib.decompressobj();pixels=decoder.decompress(bytes(packed),maximum+1)
        if len(pixels)!=maximum or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or any(pixels[p]>4 for p in range(0,maximum,1024*channels+1)):raise ValueError()
        return dict(value),len(data)
    except (ValueError,UnicodeError,zlib.error,struct.error):raise fail('preview_png_invalid') from None
