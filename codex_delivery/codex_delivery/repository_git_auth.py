"""Fixed HTTPS identities for trusted Git only; no credential acquisition API."""
from dataclasses import dataclass, field
from datetime import datetime
import base64
import hmac
import os
from pathlib import Path
import re
import stat

from .durability import digest
from .repo_manager import GitRuntime, RepositoryPolicy, _oid, _ref, _sha
from .store import DeliveryError, SHANGHAI
from .target_observation_client import decode

REGISTRATION_PATH=Path('/etc/codex-delivery/repository-git/registration.json')
POLICY_PATH=Path('/opt/codex-delivery/repository-git-policy.json')


def fail():
    raise DeliveryError('repository_git_registration_invalid') from None


def closed(value, fields):
    if type(value) is not dict or set(value)!=set(fields):fail()
    return value


def _file(path,*,private=False,optional=False):
    """Root-only no-follow read; reject races including directory-entry swaps."""
    parent=descriptor=None
    try:
        if not isinstance(path,Path) or not path.is_absolute() or any(part in {'.','..'} for part in path.parts):fail()
        parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            info=os.fstat(parent)
            if info.st_uid!=0 or info.st_mode&0o022:fail()
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        info=os.fstat(parent)
        if info.st_uid!=0 or info.st_mode&0o022 or private and stat.S_IMODE(info.st_mode)!=0o700:fail()
        descriptor=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        before=os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1
                or before.st_mode&0o022 or private and stat.S_IMODE(before.st_mode)!=0o600
                or not 1<=before.st_size<=32768):fail()
        raw=bytearray()
        while len(raw)<=before.st_size:
            block=os.read(descriptor,min(65536,before.st_size+1-len(raw)))
            if not block:break
            raw.extend(block)
        identity=lambda value:(value.st_dev,value.st_ino,value.st_uid,value.st_mode,value.st_nlink,
                               value.st_size,value.st_mtime_ns,value.st_ctime_ns)
        after=os.fstat(descriptor)
        linked=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
        if len(raw)!=before.st_size or identity(before)!=identity(after) or identity(before)!=identity(linked):fail()
        return bytes(raw)
    except FileNotFoundError:
        if optional:return None
        fail()
    except Exception:fail()
    finally:
        for fd in (descriptor,parent):
            if fd is not None:os.close(fd)


def parse_policy(raw):
    try:
        if type(raw) is not bytes or not 1<=len(raw)<=32768:fail()
        policy=closed(decode(raw),{'version','service_uid','expires_at','git_sha256','repositories','forbidden_token_sha256'})
        if (type(policy['version']) is not int or policy['version']!=1
                or type(policy['service_uid']) is not int or not 1<=policy['service_uid']<=2**31-1
                or type(policy['expires_at']) is not str):fail()
        expiry=datetime.fromisoformat(policy['expires_at'])
        if expiry.utcoffset()!=datetime.now(SHANGHAI).utcoffset() or expiry<=datetime.now(SHANGHAI):fail()
        _sha(policy['git_sha256'])
        repos=policy['repositories']
        if type(repos) is not list or not 1<=len(repos)<=32:fail()
        parsed=[]
        for item in repos:
            closed(item,{'policy','username'})
            data=closed(item['policy'],RepositoryPolicy.__dataclass_fields__)
            targets=data['publish_targets']
            if type(targets) is not list or any(type(pair) is not list or len(pair)!=2 for pair in targets):fail()
            repository=RepositoryPolicy(**dict(data,publish_targets=tuple(tuple(pair) for pair in targets)))
            if (type(item['username']) is not str or re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',item['username']) is None):fail()
            parsed.append((repository,item['username']))
        if len({p.remote_identity for p,_ in parsed})!=len(parsed) or len({p.https_url for p,_ in parsed})!=len(parsed):fail()
        fingerprints=policy['forbidden_token_sha256']
        if type(fingerprints) is not list or len(fingerprints)>128 or len(set(fingerprints))!=len(fingerprints):fail()
        for value in fingerprints:_sha(value)
        return policy,tuple(parsed)
    except Exception:fail()


@dataclass(frozen=True,repr=False)
class GitAuthBootPlan:
    service_uid:int
    _raw:bytes=field(repr=False)
    _credentials:tuple=field(repr=False)
    _root_loaded:bool=field(default=False,init=False,repr=False)

    @classmethod
    def load(cls,*,forbidden_credentials=()):
        try:
            try:REGISTRATION_PATH.lstat()
            except FileNotFoundError:return None
            raw=_file(REGISTRATION_PATH,private=True,optional=True)
            if raw is None:return None
            value=decode(raw)
            if (value=={'version':1,'enabled':False} and type(value['version']) is int and value['enabled'] is False):return None
            plan=cls.parse(raw,_file(POLICY_PATH),forbidden_credentials=forbidden_credentials)
            plan.verify()
            object.__setattr__(plan,'_root_loaded',True)
            return plan
        except Exception:fail()

    @classmethod
    def parse(cls,raw,policy_raw,*,forbidden_credentials=()):
        """Data-only tests/boot seam; verify() still requires fixed root policy."""
        try:
            if type(raw) is not bytes or not 1<=len(raw)<=32768:fail()
            registration=closed(decode(raw),{'version','enabled','policy_sha256','credentials'})
            policy,repositories=parse_policy(policy_raw)
            if (type(registration['version']) is not int or registration['version']!=1
                    or registration['enabled'] is not True or registration['policy_sha256']!=digest(policy_raw)
                    or type(forbidden_credentials) not in {tuple,list}
                    or any(type(v) not in {str,bytes} for v in forbidden_credentials)):fail()
            credentials=closed(registration['credentials'],{p.remote_identity for p,_ in repositories})
            used=[v.encode() if type(v) is str else v for v in forbidden_credentials];selected=[]
            for repository,_ in repositories:
                roles=closed(credentials[repository.remote_identity],{'read_token','publish_token'})
                for role in ('read_token','publish_token'):
                    token=roles[role]
                    if role=='publish_token' and token is None:continue
                    if (type(token) is not str or not 32<=len(token)<=4096
                            or any(not 33<=ord(c)<=126 for c in token)):fail()
                    encoded=token.encode()
                    if (digest(encoded) in policy['forbidden_token_sha256']
                            or any(hmac.compare_digest(encoded,other) for other in used)):fail()
                    used.append(encoded)
                selected.append((repository.remote_identity,roles['read_token'],roles['publish_token']))
            return cls(policy['service_uid'],policy_raw,tuple(selected))
        except Exception:fail()

    def verify(self):
        if _file(POLICY_PATH)!=self._raw:fail()
        return parse_policy(self._raw)

    @property
    def reserved_credentials(self):
        """Trusted boot memory only: never serialize, log or expose to tasks."""
        return tuple(token for _,read,publish in self._credentials for token in (read,publish) if token is not None)

    def _qualified(self,repository_plan):
        from .repository_boot import RepositoryBootPlan,_policy
        if not self._root_loaded or type(repository_plan) is not RepositoryBootPlan:fail()
        repository_plan.verify()
        repository,policies,_=_policy(repository_plan._raw)
        own,repositories=self.verify()
        if (own['service_uid']!=repository['service_uid'] or own['git_sha256']!=repository['git_sha256']
                or self.service_uid!=own['service_uid'] or repository_plan.service_uid!=self.service_uid
                or os.getuid()!=self.service_uid or os.geteuid()!=self.service_uid
                or {p.sha256 for p in policies}!={p.sha256 for p,_ in repositories}):fail()
        for token in self.reserved_credentials:
            encoded=token.encode('ascii')
            if (hmac.compare_digest(encoded,repository_plan._token.encode('ascii'))
                    or digest(encoded) in repository['forbidden_token_sha256']):fail()
        return own,repositories

    def create(self,repository_plan):
        """Only the original Repository loader supplies fixed Git verification."""
        self._qualified(repository_plan)
        return AuthenticatedGitRuntime(self,repository_plan)


class AuthenticatedGitRuntime(GitRuntime):
    """No public URL/credential injection seam; exact fixed Git grammars only."""
    def __init__(self,plan,repository_plan):
        from .repository_boot import RepositoryBootPlan
        if type(plan) is not GitAuthBootPlan or not plan._root_loaded or type(repository_plan) is not RepositoryBootPlan:fail()
        super().__init__('/usr/bin/git')
        self._plan,self._repository_plan=plan,repository_plan

    def _ready(self):
        return self._plan._qualified(self._repository_plan)

    def _selection(self,arguments):
        if (type(arguments) is not list or not arguments
                or any(type(value) is not str or '\0' in value for value in arguments)):fail()
        action=arguments[0]
        if action not in {'fetch','ls-remote','push'}:return None
        _,repositories=self._ready()
        if action=='ls-remote' and len(arguments)==4 and arguments[1]=='--refs':
            url,ref=arguments[2:];role='read_token';oid=None
        elif action=='fetch' and len(arguments)==7 and arguments[1:5]==[
                '--no-tags','--no-recurse-submodules','--no-auto-maintenance','--no-write-fetch-head']:
            url=arguments[5];ref,separator,destination=arguments[6].partition(':')
            if not separator:fail()
            if re.fullmatch(r'refs/codex/fetch/SYNC-[a-f0-9]{32}',destination) is None:fail()
            role='read_token';oid=None
        elif action=='push' and len(arguments)==6 and arguments[1:4]==[
                '--porcelain','--no-verify','--recurse-submodules=no']:
            url=arguments[4];oid,separator,ref=arguments[5].partition(':')
            if not separator:fail()
            _oid(oid);role='publish_token'
        else:fail()
        _ref(ref)
        selected=[(p,username) for p,username in repositories if p.https_url==url]
        if len(selected)!=1:fail()
        policy,username=selected[0]
        allowed={policy.baseline_ref} if action=='fetch' else {policy.baseline_ref,*dict(policy.publish_targets).values()}
        if action=='push':allowed=set(dict(policy.publish_targets).values())
        if ref not in allowed:fail()
        values={identity:(read,publish) for identity,read,publish in self._plan._credentials}
        token=values[policy.remote_identity][0 if role=='read_token' else 1]
        if token is None:fail()
        return url,username,token

    def _environment(self,home,arguments):
        environment=super()._environment(home,arguments)
        selection=self._selection(arguments)
        if selection is not None:
            url,username,token=selection
            header='Authorization: Basic '+base64.b64encode((username+':'+token).encode()).decode()
            environment.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.'+url+'.extraHeader',GIT_CONFIG_VALUE_0=header)
        return environment

    def run(self,home,repository,arguments,budget,*,lockfd,allowed=(0,)):
        self._ready()
        selection=self._selection(arguments)
        try:
            result=super().run(home,repository,arguments,budget,lockfd=lockfd,allowed=allowed)
        finally:
            self._ready()
            budget.remaining()
        if selection is not None:
            _,username,token=selection
            # Trusted Git normally sends remote errors to stderr (discarded by
            # the base runtime). Never propagate a reflected credential in an
            # unusual successful stdout response either.
            if any(value in result[1] for value in (token.encode('ascii'),
                    base64.b64encode((username+':'+token).encode('ascii')))):fail()
        return result
