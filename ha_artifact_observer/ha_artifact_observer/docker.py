"""AF_UNIX Docker client with no request method, URL or command input."""
import http.client
import math
from pathlib import Path
import re
import socket
import stat
import time
import threading

from .contract import Binding,HEX,SHA,REPOSITORY,ObserverError,decode,fail


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self,path,timeout):
        super().__init__('localhost',timeout=timeout);self._socket_path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:self.sock.connect(self._socket_path)
        except Exception:self.sock.close();raise


class DockerInspector:
    def __init__(self,socket_path,*,api_version='v1.44',timeout_seconds=3,maxbytes=1024*1024,monotonic=time.monotonic):
        if (not isinstance(socket_path,str) or not Path(socket_path).is_absolute()
                or Path(socket_path).is_symlink() or type(timeout_seconds) not in {int,float}
                or not math.isfinite(timeout_seconds) or not 0.05<=timeout_seconds<=30
                or type(maxbytes) is not int or not 4096<=maxbytes<=4*1024*1024
                or not isinstance(api_version,str) or re.fullmatch(r'v1\.[0-9]{2}',api_version) is None):
            fail('observer_docker_config_invalid')
        self._path,self._version=socket_path,api_version
        self.timeout,self.maxbytes,self.monotonic=timeout_seconds,maxbytes,monotonic

    def _inspect(self,kind,identity,deadline):
        # Closed internal selector. Neither path nor verb is ever caller input.
        if kind=='containers':
            if not isinstance(identity,str) or re.fullmatch(r'(?:app|addon)_[a-z0-9][a-z0-9_]{0,63}',identity) is None:fail('observer_binding_invalid')
        elif kind=='images':
            if not isinstance(identity,str) or SHA.fullmatch(identity) is None:fail('observer_image_invalid')
        else:fail('observer_route_invalid')
        try:
            path=Path(self._path)
            if path.is_symlink() or not stat.S_ISSOCK(path.stat().st_mode):fail('observer_socket_unavailable')
        except OSError:fail('observer_socket_unavailable')
        remaining=deadline-self.monotonic()
        if remaining<=0:fail('observer_deadline_exceeded')
        connection=_UnixConnection(self._path,remaining);timer=None
        try:
            connection.connect()
            watched=connection.sock
            def expire():
                try:watched.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            # A socket recv timeout alone is insufficient for slowly dripped
            # status/header bytes. Shut down this exact socket at the total
            # deadline, covering headers and body without any Docker action.
            remaining=deadline-self.monotonic()
            if remaining<=0:fail('observer_deadline_exceeded')
            timer=threading.Timer(remaining,expire);timer.daemon=True;timer.start()
            connection.request('GET','/'+self._version+'/'+kind+'/'+identity+'/json',headers={'Accept':'application/json','Connection':'close'})
            response=connection.getresponse()
            if response.status!=200:fail('observer_docker_rejected')
            lengths=response.headers.get_all('Content-Length',[])
            transfer=response.headers.get_all('Transfer-Encoding',[])
            if len(lengths)>1 or len(transfer)>1 or lengths and transfer:fail('observer_response_invalid')
            length=lengths[0] if lengths else None
            if length is not None and (not length.isdigit() or int(length)>self.maxbytes):fail('observer_response_too_large')
            if transfer and transfer[0].lower()!='chunked':fail('observer_response_invalid')
            body=bytearray()
            while True:
                remaining=deadline-self.monotonic()
                if remaining<=0:fail('observer_deadline_exceeded')
                # HTTPConnection may clear .sock for Connection:close; response
                # owns the same socket while its bounded body is being read.
                response.fp.raw._sock.settimeout(remaining)
                chunk=response.read1(min(65536,self.maxbytes+1-len(body)))
                if not chunk:break
                body.extend(chunk)
                if len(body)>self.maxbytes:fail('observer_response_too_large')
                if length is not None and len(body)==int(length):break
            if length is not None and len(body)!=int(length):fail('observer_response_incomplete')
            if self.monotonic()>=deadline:fail('observer_deadline_exceeded')
            value=decode(bytes(body))
            if not isinstance(value,dict):fail('observer_response_invalid')
            return value
        except ObserverError:raise
        except Exception:
            fail('observer_deadline_exceeded' if self.monotonic()>=deadline else 'observer_docker_unavailable')
        finally:
            if timer is not None:timer.cancel()
            connection.close()

    @staticmethod
    def _container(value,binding):
        state=value.get('State')
        if (not isinstance(value.get('Id'),str) or HEX.fullmatch(value['Id']) is None
                or value.get('Name')!='/'+binding.container_name or not isinstance(value.get('Image'),str)
                or SHA.fullmatch(value['Image']) is None or not isinstance(state,dict)
                or state.get('Running') is not True or state.get('Status')!='running'
                or state.get('Restarting') is not False or state.get('Dead') is not False
                or not isinstance(state.get('StartedAt'),str) or not 1<=len(state['StartedAt'])<=64):
            fail('observer_container_binding_invalid')
        return value['Id'],value['Image'],state['StartedAt']

    def inspect(self,binding,*,deadline):
        if not isinstance(binding,Binding):fail('observer_binding_invalid')
        if type(deadline) not in {int,float} or not math.isfinite(deadline):fail('observer_deadline_invalid')
        deadline=min(deadline,self.monotonic()+self.timeout)
        first=self._container(self._inspect('containers',binding.container_name,deadline),binding)
        image=self._inspect('images',first[1],deadline)
        if (image.get('Id')!=first[1] or 'RepoDigests' not in image
                or image['RepoDigests'] is not None and not isinstance(image['RepoDigests'],list)):
            fail('observer_image_binding_invalid')
        repositories=image['RepoDigests'] or []
        if len(repositories)>64:fail('observer_image_binding_invalid')
        digests=[];seen=set()
        for item in repositories:
            if not isinstance(item,str) or len(item)>512 or item.count('@')!=1:fail('observer_repo_digest_invalid')
            repository,digest=item.split('@')
            if not REPOSITORY.fullmatch(repository) or not SHA.fullmatch(digest) or item in seen:fail('observer_repo_digest_invalid')
            seen.add(item);digests.append({'kind':'oci_repo_digest','repository':repository,'digest':digest})
        second=self._container(self._inspect('containers',binding.container_name,deadline),binding)
        if second!=first:fail('observer_container_changed')
        if self.monotonic()>=deadline:fail('observer_deadline_exceeded')
        return {'container_id':first[0],'docker_image_id':{'kind':'docker_image_id','digest':first[1]},
                'oci_repo_digests':sorted(digests,key=lambda item:(item['repository'],item['digest'])),'running':True}
