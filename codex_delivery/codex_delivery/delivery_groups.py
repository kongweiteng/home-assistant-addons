"""Same-Store cross-project metadata and local historical facts, no executor.

No optional runtime, Registry, network or authorization client is imported.
Group membership grants no rights; combination acceptance and HA deployment
projection remain unavailable. Queries never mutate or refresh remote facts.
"""
from contextlib import closing
import json
import re

from .store import DeliveryError, canonical, digest, identifier, reference, _verify_group_schema

MAX_MEMBERS = 32
MAX_EDGES = 128
SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
VERSION = re.compile(r'[0-9][A-Za-z0-9.+_-]{0,63}\Z')
FIELDS = frozenset({'member_ref', 'project_ref', 'task_ref', 'task_revision',
                   'release_ref', 'version', 'release_digest', 'goal'})
OPTIONAL = frozenset({'authorization_ref', 'deployment_ref', 'target_ref'})


def fail(code='group_arguments_invalid'):
    raise DeliveryError(code) from None


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: fail('group_evidence_unavailable')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: fail('group_evidence_unavailable'))
    except (ValueError, TypeError, KeyError):
        fail('group_evidence_unavailable')


def _hash(value):
    if type(value) is not str or not SHA.fullmatch(value): fail()


def _fact(row):
    return dict(event_ref=row['event_id'], kind=row['kind'], digest=row['event_digest'])


def _command(c, owner, row):
    if row is None or row['state'] != 'confirmed': fail('group_evidence_unavailable')
    event, result = _json(row['body_json']), _json(row['result_json'])
    try:
        valid = (row['owner_ref'] == owner and event.get('owner_ref') == owner
            and event.get('event_id') == row['event_id'] and event.get('kind') == row['kind']
            and event.get('result') == result and type(result) is dict
            and digest(canonical(event).encode()) == row['event_digest']
            and row['argument_digest'] == digest(canonical(dict(kind=row['kind'], arguments=event['arguments'])).encode()))
    except (TypeError,KeyError,AttributeError):
        valid=False
    if not valid:
        fail('group_evidence_unavailable')
    return result


COMMAND = ('SELECT c.*,e.kind,e.body_json,e.event_digest,d.state FROM commands c '
           'JOIN events e USING(event_id) JOIN durability d USING(event_id) ')


class DeliveryGroups:
    def __init__(self, store):
        self.store = store
        with closing(store._connect()) as c:
            _verify_group_schema(c)

    def _members(self, members, edges):
        if (type(members) is not list or not 2 <= len(members) <= MAX_MEMBERS
                or type(edges) is not list or len(edges) > MAX_EDGES): fail()
        try: members = _json(canonical(members))
        except (TypeError, ValueError): fail()
        for m in members:
            if type(m) is not dict or not FIELDS <= m.keys() or m.keys()-FIELDS-OPTIONAL: fail()
            for field in ('member_ref', 'project_ref', 'task_ref', 'release_ref', *OPTIONAL):
                if field in m: reference(m[field])
            if type(m['task_revision']) is not int or not 1 <= m['task_revision'] <= 2**31-1: fail()
            if type(m['version']) is not str or not VERSION.fullmatch(m['version']): fail()
            _hash(m['release_digest'])
            if type(m['goal']) is not str or m['goal'] not in {'published', 'deployed'}: fail()
            # HARelease and Release7 have distinct identities. No guessed mapping.
            if m['goal'] != 'published': fail('group_goal_unavailable')
            if 'deployment_ref' in m: fail('group_goal_unavailable')
        refs = {m['member_ref'] for m in members}
        if (len(refs) != len(members) or len({m['task_ref'] for m in members}) != len(members)
                or len({m['project_ref'] for m in members}) < 2): fail('group_scope_invalid')
        graph = {r: set() for r in refs}
        seen = set()
        for edge in edges:
            if type(edge) is not dict or set(edge) != {'before', 'after'}: fail('group_dependency_invalid')
            a, b = edge['before'], edge['after']
            if type(a) is not str or type(b) is not str or a not in refs or b not in refs or a == b or (a,b) in seen:
                fail('group_dependency_invalid')
            seen.add((a,b)); graph[a].add(b)
        visiting, visited = set(), set()
        def visit(node):
            if node in visiting: fail('group_dependency_invalid')
            if node in visited: return
            visiting.add(node)
            for child in graph[node]: visit(child)
            visiting.remove(node); visited.add(node)
        for node in graph: visit(node)
        return sorted(members, key=lambda m: m['member_ref']), [dict(before=a, after=b) for a,b in sorted(seen)]

    def _scope(self, c, owner, member, *, exact=False):
        row = c.execute('SELECT t.*,p.state AS project_state,p.revision AS live_project_revision '
            'FROM tasks t JOIN projects p ON p.project_id=t.project_id AND p.owner_ref=t.owner_ref '
            'WHERE t.owner_ref=? AND t.project_id=? AND t.task_id=?',
            (owner, member['project_ref'], member['task_ref'])).fetchone()
        if row is None: fail('not_found')
        if row['task_scope'] != 'project' or row['mode'] not in {'deliver', 'deploy'}: fail('group_scope_invalid')
        changed = (row['revision'] != member['task_revision'] or row['project_revision'] != row['live_project_revision']
                   or row['project_state'] != 'active' or row['control_pending']
                   or row['state'] not in {'queued', 'running', 'completed'})
        if exact and changed: fail('revision_conflict')
        return bool(changed)

    def _release(self, c, owner, m):
        rows = c.execute(COMMAND + "WHERE c.owner_ref=? AND e.kind='software.release' "
            "AND json_extract(c.result_json,'$.release.release_ref')=?", (owner,m['release_ref'])).fetchall()
        if len(rows) != 1: fail('group_evidence_unavailable')
        row = rows[0]; result = _command(c, owner, row)
        try:
            binding, request, obs, release = (result[k] for k in ('binding','request','observation','release'))
            binding_ref = 'SB-'+digest(canonical(binding).encode())[7:]
            if (result['task_id'] != m['task_ref'] or binding['owner_ref'] != owner
                    or binding['project_ref'] != m['project_ref'] or binding['task_ref'] != m['task_ref']
                    or binding['revision'] != m['task_revision'] or binding['release_ref'] != m['release_ref']
                    or binding['version'] != m['version'] or request['binding_ref'] != binding_ref):
                fail('group_evidence_unavailable')
            if _json(row['body_json'])['arguments'] != dict(binding_ref=binding_ref,release_digest=m['release_digest']):
                fail('group_evidence_unavailable')
            if 'target_ref' in m and m['target_ref'] != binding['target_ref']: fail('group_evidence_unavailable')
            original = c.execute(COMMAND + "WHERE c.owner_ref=? AND e.kind='software.bind' "
                "AND json_extract(c.result_json,'$.binding_ref')=?", (owner,binding_ref)).fetchall()
            if len(original) != 1 or _command(c,owner,original[0]) != dict(task_id=m['task_ref'],binding_ref=binding_ref,binding=binding):
                fail('group_evidence_unavailable')
            buildrow = c.execute(COMMAND + "WHERE c.owner_ref=? AND c.idempotency_key=? AND e.kind='software.build'",
                (owner,'software-build:'+binding_ref)).fetchone()
            built = _command(c,owner,buildrow)
            if built != dict(task_id=m['task_ref'],request=request,observation=obs): fail('group_evidence_unavailable')
            if _json(buildrow['body_json'])['arguments'] != dict(request_digest=obs['request_digest']):
                fail('group_evidence_unavailable')
            for field, expected in dict(owner_ref=owner,task_ref=m['task_ref'],revision=m['task_revision'],
                project_ref=m['project_ref'],release_ref=m['release_ref'],version=m['version'],
                target_ref=binding['target_ref'],permissions_digest=binding['permissions_digest']).items():
                if request[field] != expected: fail('group_evidence_unavailable')
            if set(obs) != {'request_digest','build_ref','artifact_identity','permissions_digest','qualification_digest','evidence_digest'}:
                fail('group_evidence_unavailable')
            if obs['request_digest'] != digest(canonical(request).encode()): fail('group_evidence_unavailable')
            reference(obs['build_ref'])
            for field in ('request_digest','permissions_digest','qualification_digest','evidence_digest'): _hash(obs[field])
            identity = obs['artifact_identity']
            if type(identity) is not dict: fail('group_evidence_unavailable')
            kind = identity.get('kind')
            if (kind not in {'docker_image_id','oci_repo_digest'}
                    or set(identity) != ({'kind','digest'} if kind=='docker_image_id' else {'kind','repository','digest'})):
                fail('group_evidence_unavailable')
            if kind=='oci_repo_digest' and (type(identity['repository']) is not str or re.fullmatch(
                    r'[a-z0-9][a-z0-9.:-]*(?:/[a-z0-9][a-z0-9._-]*)+',identity['repository']) is None):
                fail('group_evidence_unavailable')
            _hash(identity['digest'])
            expected = dict(schema_version=2,release_ref=m['release_ref'],version=m['version'],
                source_digest=request['source_digest'],artifact_identity=identity,
                permissions_digest=obs['permissions_digest'],qualification_digest=obs['qualification_digest'])
            if (release != expected or obs['permissions_digest'] != binding['permissions_digest']
                    or digest(canonical(release).encode()) != m['release_digest']): fail('group_evidence_unavailable')
            for field in ('source_digest','permissions_digest','qualification_digest'): _hash(release[field])
            return result, [_fact(row),_fact(buildrow),_fact(original[0])]
        except (KeyError, TypeError, ValueError, DeliveryError):
            fail('group_evidence_unavailable')

    def _project(self, c, owner, m):
        changed = self._scope(c,owner,m)
        result, facts = self._release(c,owner,m)
        try:
            binding = result['binding']; publications = result['publications']
            targets = binding['publish_targets']
            candidates = {v['repo_id']:v for v in result['request']['candidates']}
            if (not targets or len(publications) != len(targets)
                    or {p['repo_id'] for p in publications} != set(targets)):
                fail('group_goal_unavailable')
            states = []
            for item in publications:
                repo, pub = item['repo_id'], item['publication_ref']; reference(pub)
                publication = c.execute('SELECT * FROM repository_publications WHERE owner_ref=? AND publication_ref=?', (owner,pub)).fetchone()
                candidate = candidates[repo]
                if (publication is None or publication['task_id'] != m['task_ref']
                        or publication['task_revision'] != m['task_revision'] or publication['repo_id'] != repo
                        or any(publication[f] != candidate[f] for f in ('candidate_ref','candidate_oid','policy_digest','validation_digest'))):
                    fail('group_evidence_unavailable')
                stored=c.execute('SELECT * FROM repository_candidates WHERE owner_ref=? AND candidate_ref=?',
                    (owner,candidate['candidate_ref'])).fetchone()
                if (stored is None or stored['task_id']!=m['task_ref'] or stored['task_revision']!=m['task_revision']
                        or stored['repo_id']!=repo or any(stored[f]!=candidate[f] for f in
                        ('baseline_oid','candidate_oid','policy_digest'))): fail('group_evidence_unavailable')
                # Original immutable prepare event binds the actual destination.
                prepared = c.execute(COMMAND + "WHERE c.owner_ref=? AND e.kind='repository.publish.prepare' "
                    "AND json_extract(c.result_json,'$.publication_ref')=?", (owner,pub)).fetchall()
                if len(prepared) != 1: fail('group_evidence_unavailable')
                prep = _command(c,owner,prepared[0])
                args = _json(prepared[0]['body_json'])['arguments']
                if (prep != dict(task_id=m['task_ref'],repo_id=repo,publication_ref=pub,
                        state='prepared',candidate_oid=publication['candidate_oid'])
                        or args.get('task_ref') != m['task_ref'] or args.get('expected_revision') != m['task_revision']
                        or args.get('repo_ref') != repo or args.get('candidate_ref') != candidate['candidate_ref']
                        or args.get('target_alias') != targets[repo]): fail('group_evidence_unavailable')
                last = c.execute(COMMAND + "WHERE c.owner_ref=? AND e.kind IN "
                    "('repository.publish.result','repository.publish.reconcile') "
                    "AND json_extract(c.result_json,'$.publication_ref')=? ORDER BY e.sequence DESC LIMIT 1", (owner,pub)).fetchone()
                if last is None or last['state'] != 'confirmed':
                    states.append('unknown'); continue
                known = _command(c,owner,last)
                if (known.get('task_id') != m['task_ref'] or known.get('repo_id') != repo
                        or known.get('state') != publication['state'] or known.get('remote_oid') != publication['remote_oid']):
                    fail('group_evidence_unavailable')
                state = publication['state']
                if state == 'confirmed' and publication['remote_oid'] != publication['candidate_oid']:
                    fail('group_evidence_unavailable')
                states.append('succeeded' if state == 'confirmed' else 'failed' if state == 'conflict' else 'unknown')
                facts.extend([_fact(prepared[0]),dict(_fact(last),publication_ref=pub)])
            state = 'unknown' if 'unknown' in states else 'failed' if 'failed' in states else 'succeeded'
            success = state == 'succeeded'
            return dict(m,state='blocked' if changed and success else state,
                reason='member_scope_changed' if changed else 'historical_local_facts',
                historical_success=success,facts=facts,authorization_state='not_evaluated')
        except (KeyError,TypeError,ValueError):
            fail('group_evidence_unavailable')

    def create(self, owner, key, *, members, edges, acceptance_ref):
        reference(owner); reference(key); reference(acceptance_ref)
        members,edges = self._members(members,edges)
        arguments = dict(members=members,edges=edges,acceptance_ref=acceptance_ref)
        self.store.verify_event_chain()
        def apply(c,timestamp):
            projections=[]
            for m in members:
                self._scope(c,owner,m,exact=True)
                projections.append(self._project(c,owner,m))
            group = identifier('DG')
            c.execute("INSERT INTO delivery_groups(group_id,owner_ref,revision,control,acceptance_ref,created_at,updated_at) "
                "VALUES(?,?,1,'active',?,?,?)", (group,owner,acceptance_ref,timestamp,timestamp))
            for m in members:
                c.execute('INSERT INTO delivery_group_members VALUES(?,?,?,?,?,?,?)',
                    (group,m['member_ref'],owner,m['project_ref'],m['task_ref'],m['task_revision'],canonical(m)))
            for e in edges: c.execute('INSERT INTO delivery_group_edges VALUES(?,?,?)',(group,e['before'],e['after']))
            c.execute('UPDATE delivery_groups SET sealed=1 WHERE group_id=?',(group,))
            return dict(group_ref=group,revision=1,members=projections)
        return self.store._mutation(owner,key,'group.create',arguments,apply)

    def _group(self,c,owner,group_ref):
        reference(owner); reference(group_ref)
        group = c.execute('SELECT * FROM delivery_groups WHERE owner_ref=? AND group_id=?',(owner,group_ref)).fetchone()
        if group is None: fail('not_found')
        if group['sealed']!=1: fail('group_evidence_unavailable')
        members=[]
        for row in c.execute('SELECT * FROM delivery_group_members WHERE group_id=? ORDER BY member_ref',(group_ref,)):
            m=_json(row['document_json'])
            if (type(m) is not dict or row['owner_ref']!=owner or any(row[column]!=m.get(field) for column,field in
                    (('member_ref','member_ref'),('project_id','project_ref'),('task_id','task_ref'),('task_revision','task_revision')))):
                fail('group_evidence_unavailable')
            members.append(m)
        for m in members: self._scope(c,owner,m)
        edges = [dict(before=r[0],after=r[1]) for r in c.execute('SELECT before_ref,after_ref FROM delivery_group_edges WHERE group_id=? ORDER BY before_ref,after_ref',(group_ref,))]
        self._members(members,edges)
        return group,members,edges

    def _view(self,c,owner,group_ref):
        group,members,edges = self._group(c,owner,group_ref)
        rows = []
        for m in members:
            try: rows.append(self._project(c,owner,m))
            except DeliveryError as error:
                if error.code in {'not_found','group_scope_invalid'}: raise
                rows.append(dict(m,state='unknown',reason='group_evidence_unavailable',historical_success=False,
                                 facts=[],authorization_state='not_evaluated'))
        # Preserve confirmed successes from earlier group observations even if
        # later evidence becomes unavailable. No claim is cleared or replayed.
        prior = c.execute(COMMAND + "WHERE c.owner_ref=? AND d.state='confirmed' AND e.kind IN ('group.create','group.observe') "
            "AND json_extract(c.result_json,'$.group_ref')=? ORDER BY e.sequence DESC LIMIT 1",(owner,group_ref)).fetchone()
        if prior is not None and prior['state']=='confirmed':
            old = _command(c,owner,prior)
            earlier = {v['member_ref']:v for v in old['members']}
            for row in rows:
                previous = earlier.get(row['member_ref'])
                if previous and previous['historical_success']:
                    row['historical_success']=True
                    if not row['facts']: row['facts']=previous['facts']
        by_ref={r['member_ref']:r for r in rows}
        for row in rows:
            predecessors=[e['before'] for e in edges if e['after']==row['member_ref']]
            row['dependency_blocked']=(not row['historical_success'] and
                any(not by_ref[p]['historical_success'] for p in predecessors))
            # Dependency status is distinct from external uncertainty: never
            # replace unknown/failed evidence, or erase an already effected goal.
            if row['dependency_blocked'] and row['state']=='pending':
                row.update(state='blocked',reason='dependency_unconfirmed')
        unknown = any(r['state']=='unknown' for r in rows)
        failed = any(r['state'] in {'blocked','failed','unknown','pending'} for r in rows)
        any_success = any(r['historical_success'] for r in rows)
        state = ('partially_completed' if any_success and failed else 'recovery_required' if unknown
                 else 'blocked' if failed else 'awaiting_acceptance')
        if group['control']!='active' and state not in {'partially_completed','recovery_required'}: state=group['control']
        latest=c.execute("SELECT d.state FROM events e JOIN durability d USING(event_id) WHERE e.owner_ref=? "
            "AND e.kind IN ('group.create','group.observe','group.control') AND json_extract(e.body_json,'$.result.group_ref')=? "
            "ORDER BY e.sequence DESC LIMIT 1",(owner,group_ref)).fetchone()
        return dict(group_ref=group_ref,revision=group['revision'],control=group['control'],state=state,
            requires_reconciliation=unknown,recovery_read_only=self.store.recovery_mode,
            members=rows,edges=edges,acceptance_ref=group['acceptance_ref'],combination_accepted=False,
            durability='replicated' if latest and latest[0]=='confirmed' else 'local_only')

    def status(self,owner,group_ref):
        self.store.verify_event_chain()
        with closing(self.store._connect()) as c:
            c.execute('BEGIN')
            return self._view(c,owner,group_ref)

    def list(self,owner,*,offset=0,limit=20):
        reference(owner)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100: fail('pagination_invalid')
        self.store.verify_event_chain()
        with closing(self.store._connect()) as c:
            c.execute('BEGIN')
            total=c.execute('SELECT count(*) FROM delivery_groups WHERE owner_ref=?',(owner,)).fetchone()[0]
            refs=c.execute('SELECT group_id FROM delivery_groups WHERE owner_ref=? ORDER BY created_at,group_id LIMIT ? OFFSET ?', (owner,limit,offset)).fetchall()
            summaries=('group_ref','revision','control','state','requires_reconciliation',
                       'recovery_read_only','acceptance_ref','combination_accepted','durability')
            items=[{k:v for k,v in self._view(c,owner,r[0]).items() if k in summaries} for r in refs]
        return dict(items=items,total=total,has_more=offset+len(items)<total,next_offset=offset+len(items) if offset+len(items)<total else None)

    def events(self,owner,group_ref,*,after=0,limit=20):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100: fail('pagination_invalid')
        self.store.verify_event_chain()
        with closing(self.store._connect()) as c:
            c.execute('BEGIN'); self._group(c,owner,group_ref)
            where="e.owner_ref=? AND e.kind IN ('group.create','group.control','group.observe') AND json_extract(e.body_json,'$.result.group_ref')=? AND e.sequence>?"
            args=(owner,group_ref,after)
            total=c.execute('SELECT count(*) FROM events e WHERE '+where,args).fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT e.sequence,e.event_id AS event_ref,e.kind,e.created_at,d.state AS durability FROM events e JOIN durability d USING(event_id) WHERE '+where+' ORDER BY e.sequence LIMIT ?',(*args,limit))]
        return dict(items=rows,total=total,has_more=len(rows)<total,next_after=rows[-1]['sequence'] if rows else after)

    def _revision(self,c,owner,group_ref,expected):
        group,_,_=self._group(c,owner,group_ref)
        if type(expected) is not int or group['revision']!=expected: fail('revision_conflict')
        return group

    def control(self,owner,key,*,group_ref,expected_revision,action):
        if type(action) is not str or action not in {'pause','resume','cancel'}: fail()
        self.store.verify_event_chain()
        args=dict(group_ref=group_ref,expected_revision=expected_revision,action=action)
        def apply(c,timestamp):
            group=self._revision(c,owner,group_ref,expected_revision)
            if group['control']=='cancelled': fail('group_scope_invalid')
            control={'pause':'paused','resume':'active','cancel':'cancelled'}[action]
            revision=group['revision']+1
            c.execute('UPDATE delivery_groups SET control=?,revision=?,updated_at=? WHERE group_id=?',(control,revision,timestamp,group_ref))
            return dict(group_ref=group_ref,revision=revision,control=control)
        return self.store._mutation(owner,key,'group.control',args,apply)

    def observe(self,owner,key,*,group_ref,expected_revision):
        self.store.verify_event_chain()
        args=dict(group_ref=group_ref,expected_revision=expected_revision)
        def apply(c,timestamp):
            self._revision(c,owner,group_ref,expected_revision)
            view=self._view(c,owner,group_ref); view['revision']+=1
            c.execute('UPDATE delivery_groups SET revision=?,updated_at=? WHERE group_id=?',(view['revision'],timestamp,group_ref))
            return view
        return self.store._mutation(owner,key,'group.observe',args,apply)
