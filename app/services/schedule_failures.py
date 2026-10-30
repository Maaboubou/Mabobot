"""Conservative failure classification; never include raw errors in chat notices."""
import re


def classify_failure(error):
    chain, current = [], error
    for _ in range(5):
        if current is None:
            break
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = ' '.join(str(item) for item in chain).lower()
    if any(word in text for word in ('input file', 'input attachment', 'checksum', 'symbolic link', '资源', '任务文件', '交付目录', '附件')):
        return 'resource', False
    if any(word in text for word in ('permission', 'unauthorized', 'forbidden', '权限', '未开启', '已关闭', '禁用')):
        return 'permission', False
    if any(word in text for word in ('profile', '配置', '未启动', 'not configured', 'unavailable profile', 'authentication', '登录')):
        return 'configuration', False
    transient = any(isinstance(item, (TimeoutError, ConnectionError)) for item in chain) or bool(re.search(
        r'timed?\s*out|timeout|connection (?:reset|refused|closed)|temporarily unavailable|service unavailable|\b(?:429|502|503|504)\b|连接.*(?:超时|断开|失败)', text))
    if transient:
        return 'connection', True
    return 'execution', False


def failure_text(title, category, *, uncertain=False, exhausted=False):
    intro = f'刚才安排的「{title}」这次没能确认完成。' if uncertain else f'刚才安排的「{title}」这次没完成。'
    if uncertain:
        return intro + '执行中断了，为免重复处理，我没有自动重做。需要先核对这次的结果。'
    detail = {
        'resource': '读取或处理参考文件时出了问题，处理好文件后可以再试一次。',
        'permission': '目前没有所需权限，权限恢复后可以再试一次。',
        'configuration': '执行环境暂时不可用，处理好后可以再试一次。',
        'connection': '连接一直没恢复，已经重试过，这次先停在这里。' if exhausted else '连接出了问题，这次先停在这里。',
        'execution': '执行时出了点问题，这次没有得到可交付的结果。',
    }
    return intro + detail[category]
