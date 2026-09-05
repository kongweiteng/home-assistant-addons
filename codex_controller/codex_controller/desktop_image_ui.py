"""Bounded, memory-only image drafts and lazy authenticated conversation images."""

IMAGE_UI_JS = r"""
const imageState = {drafts: Object.create(null), pending: Object.create(null), busy: Object.create(null), cache: new Map(), returnFocus: null};
function currentAttachments(ref = state.selectedThread) { return imageState.drafts[ref] || []; }
function setImageOpen(url = '', caption = '') {
  const open = Boolean(url);
  if (open) imageState.returnFocus = document.activeElement;
  q('imageDialog').classList.toggle('hidden', !open);
  if (open) q('fullImage').src = url; else q('fullImage').removeAttribute('src');
  q('fullImageCaption').textContent = caption;
  syncDialogBackground();
  if (open) q('closeImage').focus(); else restoreFocus(imageState.returnFocus);
}
function renderAttachments(enabled = !q('submitDirection').disabled) {
  const items = currentAttachments();
  const pending = imageState.pending[state.selectedThread];
  const busy = imageState.busy[state.selectedThread];
  const supported = hasCapability('image_input_v1');
  q('addImage').disabled = !supported || !writeAvailable() || !state.detail || Boolean(pending || busy) || items.length >= 4;
  q('imageHelp').textContent = !supported ? '此 Runner 尚不支持图片，升级后可用' : '最多 4 张 · 上传前压缩至每张 64 KiB · 请预览确认文字清晰';
  const tray = q('attachmentTray');
  tray.replaceChildren();
  tray.classList.toggle('hidden', !items.length);
  for (const item of items) {
    if (item.image_ref && Date.parse(item.expires_at) <= Date.now()) item.error = '图片已过期，请重试上传';
    const card = document.createElement('div'); card.className = 'attachment';
    const preview = document.createElement('button'); preview.type = 'button'; preview.className = 'preview'; preview.setAttribute('aria-label', `预览 ${item.name}`);
    if (item.url) { const img = document.createElement('img'); img.src = item.url; img.alt = item.name; preview.append(img); preview.onclick = () => setImageOpen(item.url, `${item.name} · 实际发送版本`); }
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'remove'; remove.textContent = '移除'; remove.setAttribute('aria-label', `移除 ${item.name}`); remove.disabled = Boolean(pending || busy);
    remove.onclick = () => { imageState.drafts[state.selectedThread] = items.filter(value => value !== item); item.removed = true; if (item.url) URL.revokeObjectURL(item.url); if (state.detail) renderComposer(state.detail); };
    const status = document.createElement('span'); status.className = 'attachment-status'; status.textContent = item.error || (item.image_ref ? '图片已上传 · 待发送' : '准备并上传中…'); status.title = status.textContent;
    card.append(preview, remove, status);
    if (item.error && item.blob) { const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '重试上传'; retry.disabled = Boolean(pending || busy); retry.onclick = () => void uploadAttachment(item); card.append(retry); }
    tray.append(card);
  }
  if (items.length && (items.some(item => !item.image_ref || item.error) || (composerAction(state.detail) === 'steer' && state.mode === 'native') || !supported)) q('submitDirection').disabled = true;
  if (items.length && state.mode === 'native' && state.detail?.status === 'active') q('imageHelp').textContent = '快速调整不支持图片，请切换安全调整；附件已保留';
  let check = q('checkPendingMessage');
  if (!check) { check = document.createElement('button'); check.id = 'checkPendingMessage'; check.type = 'button'; q('imageHelp').insertAdjacentElement('afterend', check); }
  check.classList.toggle('hidden', !pending); check.disabled = Boolean(busy) || !navigator.onLine;
  check.textContent = '检查发送结果（同一请求）';
  check.onclick = () => { if (pending) void submitAction(pending.action, pending.extra, pending); };
  if (pending) q('composerFeedback').textContent = '发送结果待确认，草稿与请求编号已保留；不会自动重发';
  const queue = q('queueMessageButton'); if (queue && items.length) queue.disabled = true;
}
async function compressImage(file) {
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('仅支持 PNG、JPEG、WebP');
  if (file.size > 10 * 1024 * 1024) throw new Error('原图不能超过 10 MiB');
  const url = URL.createObjectURL(file);
  try {
    const img = new Image(); img.src = url; await img.decode();
    if (!img.naturalWidth || img.naturalWidth * img.naturalHeight > 40000000) throw new Error('图片像素过大，请先裁剪');
    const canvas = document.createElement('canvas');
    for (const bound of [1600, 1280, 1024, 800]) {
      const scale = Math.min(1, bound / Math.max(img.naturalWidth, img.naturalHeight));
      canvas.width = Math.max(1, Math.round(img.naturalWidth * scale)); canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
      const ctx = canvas.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      for (const quality of [0.9, 0.78, 0.65]) {
        const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', quality));
        if (blob && blob.size <= 65536 && ['image/png', 'image/jpeg', 'image/webp'].includes(blob.type)) return blob;
      }
    }
    throw new Error('压缩后仍超过 64 KiB，请裁剪关键区域再添加');
  } finally { URL.revokeObjectURL(url); }
}
async function uploadAttachment(item) {
  item.error = ''; if (state.selectedThread === item.thread) renderAttachments();
  try {
    if (!item.blob) { item.blob = await compressImage(item.file); item.file = null; if (!item.removed) item.url = URL.createObjectURL(item.blob); }
    if (item.removed) return;
    const bytes = new Uint8Array(await item.blob.arrayBuffer());
    let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
    const result = await jsonFetch(`${API}/images`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify({host_ref: item.host, request_id: item.request, mime_type: item.blob.type, data_base64: btoa(binary)})});
    if (!/^IM-[a-f0-9]{32}$/.test(result.image_ref)) throw new Error('图片上传响应无效');
    item.image_ref = result.image_ref; item.expires_at = result.expires_at; item.error = '';
  } catch (error) { item.error = error.message; }
  finally { if (state.selectedThread === item.thread && state.detail) renderComposer(state.detail); }
}
async function addImages(files) {
  if (q('addImage').disabled) return;
  const ref = state.selectedThread;
  const items = imageState.drafts[ref] ||= [];
  const selected = Array.from(files);
  if (selected.length + items.length > 4) { q('composerFeedback').textContent = '每条消息最多 4 张图片，请减少选择'; return; }
  const added = selected.map(file => ({file, name: file.name || '粘贴图片', request: requestId(), host: state.selectedHost, thread: ref, removed: false}));
  items.push(...added); renderAttachments();
  // One bounded upload at a time; never put image bytes in a realtime frame.
  for (const item of added) await uploadAttachment(item);
}
function renderImageHistory(root, messages = state.detail?.image_messages || []) {
  for (const message of messages) {
    const refs = (message.image_refs || []).filter(ref => /^IM-[a-f0-9]{32}$/.test(ref));
    if (!refs.length) continue;
    const confirmed = message.state === 'confirmed' || message.receipt?.delivery_stage === 'mac_confirmed';
    const group = messageNode('user', message.input || '图片消息', {label: confirmed ? 'Mac 已确认图文消息' : '图文消息 · 待核对回执'});
    const info = document.createElement('small'); info.textContent = `${formatTime(message.created_at)} · ${confirmed ? 'Mac 已确认' : statusText(message.state)}`; group.append(info);
    const tray = document.createElement('div'); tray.className = 'image-history';
    for (const ref of refs) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '加载图片';
      button.setAttribute('aria-label', '加载并查看对话图片');
      const saved = imageState.cache.get(ref);
      if (saved && Date.parse(saved.expires) > Date.now()) { const img = document.createElement('img'); img.src = saved.url; img.alt = '对话图片'; button.replaceChildren(img); }
      button.onclick = async () => {
        button.disabled = true; button.textContent = '加载中…';
        try {
          let cached = imageState.cache.get(ref);
          if (!cached || Date.parse(cached.expires) <= Date.now()) {
            const image = await jsonFetch(`${API}/images/${ref}`, {headers: {'X-CSRF-Token': state.csrf}});
            if (!['image/png', 'image/jpeg', 'image/webp'].includes(image.mime_type) || image.data_base64.length > 87384 || !/^[A-Za-z0-9+/]*={0,2}$/.test(image.data_base64)) throw new Error('图片数据无效');
            cached = {url: `data:${image.mime_type};base64,${image.data_base64}`, expires: image.expires_at}; imageState.cache.set(ref, cached);
            if (imageState.cache.size > 16) imageState.cache.delete(imageState.cache.keys().next().value);
          }
          const img = document.createElement('img'); img.src = cached.url; img.alt = '对话图片'; button.replaceChildren(img);
          setImageOpen(cached.url, '对话图片 · 上传保存 24 小时');
        } catch (error) { button.textContent = `${error.message} · 点击重试`; }
        finally { button.disabled = false; }
      };
      tray.append(button);
    }
    group.append(tray); root.append(group);
  }
}
function initImageUi() {
  // The project picker is a modal, not part of the inert background.
  const panel = q('projectPanel'); document.body.append(panel); panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true');
  q('addImage').onclick = () => q('imageInput').click();
  q('imageInput').onchange = () => { const files = Array.from(q('imageInput').files); q('imageInput').value = ''; void addImages(files); };
  q('composerInput').addEventListener('paste', event => { const files = Array.from(event.clipboardData?.files || []); if (files.length && !q('addImage').disabled) { event.preventDefault(); void addImages(files); } });
  q('closeImage').onclick = () => setImageOpen();
  window.addEventListener('beforeunload', () => { for (const items of Object.values(imageState.drafts)) for (const item of items) if (item.url) URL.revokeObjectURL(item.url); imageState.cache.clear(); });
}
"""
