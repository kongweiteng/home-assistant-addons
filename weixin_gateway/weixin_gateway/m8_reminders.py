"""Owner-only deterministic acknowledgement of a charging reminder."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import aiohttp

ENDPOINT = 'http://local-m8-charge-planner:8099/api/reminders/command'
COMMANDS = frozenset({'忽略本次充电', '忽略这次充电', '本次不用提醒充电'})


DEFER_RE = re.compile(r'^(?:充电)?(?:延迟到)?(今晚|今天|明天|晚上)?\s*(\d{1,2})(?:[:：](\d{2})|点(?:(半)|(\d{1,2})分?)?)(?:再提醒(?:我)?|提醒我)$')


def is_reminder_command(text: object) -> bool:
    if not isinstance(text, str):
        return False
    normalized = text.strip().rstrip('。！!').strip()
    return normalized in COMMANDS or DEFER_RE.fullmatch(normalized) is not None


def parse_command(text: str, now: dt.datetime | None = None) -> dict:
    normalized = text.strip().rstrip('。！!').strip()
    if normalized in COMMANDS:
        return {'command': 'ignore_current_charge'}
    match = DEFER_RE.fullmatch(normalized)
    if not match:
        raise ValueError('请回复“忽略本次充电”或“22点再提醒”。')
    qualifier, hour, colon_minutes, half, chinese_minutes = match.groups()
    hour = int(hour)
    minute = 30 if half else int(colon_minutes or chinese_minutes or 0)
    if qualifier in {'今晚', '晚上'} and hour < 12:
        hour += 12
    if not 8 <= hour < 23 or not 0 <= minute <= 59:
        raise ValueError('充电催促仅安排在08:00至23:00前，请重新指定时间。')
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone(dt.timedelta(hours=8)))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if qualifier == '明天':
        target += dt.timedelta(days=1)
    if not current < target <= current + dt.timedelta(hours=24):
        raise ValueError('该时间已过或超过24小时，请明确回复例如“明天08点再提醒”。')
    return {'command': 'defer_current_charge', 'remind_at': target.isoformat()}


async def handle_reminder_reply(token: str, identity_id: str, message_id: str, text: str) -> str:
    if len(token) < 32:
        return '充电提醒回复功能尚未就绪，本次未忽略。'
    try:
        command = parse_command(text)
    except ValueError as error:
        return str(error)
    payload = {
        **command,
        'request_id': hashlib.sha256((identity_id + ':' + message_id).encode()).hexdigest(),
        'issued_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
            async with session.post(ENDPOINT, json=payload, headers={'Authorization': 'Bearer ' + token}, allow_redirects=False) as response:
                if response.status != 200:
                    return '充电提醒服务暂时无法确认，本次未确认忽略，请稍后重试。'
                result = await response.json()
        if result.get('ignored') is not True and result.get('deferred') is not True:
            return '本次充电提醒未忽略，请稍后重试。'
        until = dt.datetime.fromisoformat(result['until'].replace('Z', '+00:00')).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime('%m月%d日 %H:%M')
        if result.get('deferred') is True:
            return f'已延迟到 {until} 再检查充电需求，此前暂停本轮催促；届时已充电或已预约则保持静默。'
        return f'已忽略本轮充电催促，最晚 {until} 自动恢复；电量恢复后也会结束本次忽略。充电故障仍会提醒。'
    except Exception:
        # Never expose URL, authentication material or untrusted response bodies.
        return '暂时无法确认充电提醒设置是否生效，请稍后重试原指令。'
