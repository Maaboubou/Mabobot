"""Current-request image staging and ACP multimodal prompt conversion."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path

from PIL import Image
from app.services.execution_contracts import ExecutionError, canonical


def prepare_prompt(data, request_dir):
    messages = copy.deepcopy(data)
    images = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get('content'), list):
                continue
            parts = []
            for part in message['content']:
                if part.get('type') != 'image_url':
                    parts.append(part)
                    continue
                value = part.get('image_url')
                url = value.get('url', '') if isinstance(value, dict) else ''
                if not isinstance(url, str) or not url.startswith('data:image/') or ';base64,' not in url:
                    raise ExecutionError('invalid_image_input', '图片必须由当前聊天提供，不能使用远程图片地址')
                if len(url) > 7 * 1024 * 1024 or len(images) >= 16:
                    raise ExecutionError('image_input_limit', '图片过大或超过 8 张，请缩小图片或分批发送')
                try:
                    raw = base64.b64decode(url.split(';base64,', 1)[1], validate=True)
                    with Image.open(io.BytesIO(raw)) as picture:
                        fmt = picture.format
                        if picture.width * picture.height > 36_000_000:
                            raise ValueError('pixel limit')
                        picture.verify()
                    mime, suffix = {'PNG': ('image/png', '.png'), 'JPEG': ('image/jpeg', '.jpg'),
                        'WEBP': ('image/webp', '.webp'), 'GIF': ('image/gif', '.gif')}[fmt]
                except (ValueError, OSError, KeyError, Image.DecompressionBombError) as exc:
                    raise ExecutionError('invalid_image_input', '图片损坏、尺寸过大或格式不受支持') from exc
                directory = Path(request_dir) / 'inputs'
                if directory.is_symlink():
                    raise ExecutionError('invalid_image_input', '图片输入目录不能是链接')
                directory.mkdir(exist_ok=True)
                name = f'image-{len(images)//2+1}-' + hashlib.sha256(raw).hexdigest()[:16] + suffix
                with (directory / name).open('xb') as stream:
                    stream.write(raw)
                reference = 'inputs/' + name
                index = len(images) // 2 + 1
                parts.append({'type': 'text', 'text': f'[当前请求图片 {index}；编辑原图请调用 images__generate，reference_paths 使用 {reference}；原图只属于本次请求。]'})
                images.extend([{'type': 'text', 'text': f'当前请求图片 {index}，原图引用：{reference}'},
                    {'type': 'image', 'mimeType': mime, 'data': base64.b64encode(raw).decode('ascii')}])
            message['content'] = parts
    prompt = [{'type': 'text', 'text': messages if isinstance(messages, str) else canonical(messages)}, *images]
    if len(canonical(prompt).encode()) > 7 * 1024 * 1024:
        raise ExecutionError('image_input_limit', '本轮输入超过传输上限，请缩小图片或分批发送')
    return prompt


def native_error_code(error):
    value = json.dumps(error, ensure_ascii=False, default=str).lower()
    if any(marker in value for marker in (
        'bio_policy', 'content_policy_violation', 'content_filter',
        'safety_policy_violation',
    )):
        return 'content_policy'
    return 'native_error'


def native_error_detail(method, error):
    """Expose a useful classification, never echo native payloads or credentials."""
    serialized = json.dumps(error, ensure_ascii=False, default=str)
    value = serialized.lower()
    category = '内容安全限制' if native_error_code(error) == 'content_policy' else next((label for markers, label in [
        (('context length', 'context_length', 'too many tokens', 'token limit'), '上下文超限'),
        (('image', 'vision', 'mime'), '图片输入'),
        (('too large', 'payload', 'body limit'), '请求过大'),
        (('unauthorized', 'authentication', '401'), '认证失败'),
        (('rate limit', 'quota', '429'), '额度或频率限制'),
        (('model not found', 'unsupported model'), '模型不可用'),
    ] if any(marker in value for marker in markers)), '原生请求校验失败')
    code = error.get('code') if isinstance(error, dict) else None
    code = str(code) if isinstance(code, int) else '未知'
    identity = hashlib.sha256(serialized.encode()).hexdigest()[:12]
    return f'OpenCode {method} 拒绝请求：{category}（错误码 {code}，诊断 {identity}）'
