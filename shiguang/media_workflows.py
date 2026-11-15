"""Type-based proposer and separate executor review; routine decisions need no model call."""
import json


def propose(analysis, profile='standard'):
    music = analysis.get('library') == 'Music' or analysis.get('folder') == 'Music Videos'
    educational = analysis.get('folder') in {'Tutorials', 'Talks', 'Documentaries', 'News'}
    entertainment = analysis.get('library') in {'TV', 'Movies'}
    if music:
        kind, reason = '音乐', '保留音视频和现成字幕，跳过语音转写与长文总结'
    elif entertainment and profile != 'deep':
        kind, reason = '影视', '优先归档与现成字幕，避免对整集影视重复转写和总结'
    elif educational:
        kind, reason = '知识内容', '字幕优先；缺少字幕时转写，再生成总结与章节'
    else:
        kind, reason = '普通视频', '依据已有内容分类与处理目标决定是否深入分析'
    semantic = not music and profile != 'essential' and (profile == 'deep' or not entertainment and (
        educational or analysis.get('needs_transcript') is True))
    return {'type': kind, 'semantic': semantic, 'reason': reason,
            'steps': ['检查文件与归档', '整理已有字幕'] + (['按需转写', '生成或更新总结', '生成章节', '核验实际产物'] if semantic else ['核验实际产物'])}


def review(row, actions, profile='standard'):
    """The reviewer checks permissions/dependencies independently of a proposed step list."""
    try:
        analysis = json.loads(row['analysis'] or '{}')
        analysis = analysis if isinstance(analysis,dict) else {}
    except (ValueError,TypeError):
        analysis = {}
    reasons = []
    if row['status'] != 'done' or row['ref']:
        reasons.append('下载未完成或使用共享源，不执行内容改写')
    music = analysis.get('library') == 'Music' or analysis.get('folder') == 'Music Videos'
    for action in actions:
        if action['kind'] not in {'steward_index','transcribe','summarize','chapters'}:
            reasons.append('方案包含未授权的动作')
        if (music or profile == 'essential' or analysis.get('library') in {'TV','Movies'} and profile != 'deep') and action['kind'] != 'steward_index':
            reasons.append('该视频类型或处理档位不需要额外语音分析')
    if any(a['kind']=='summarize' for a in actions) and any(a['kind']=='steward_index' and a['part']==0 for a in actions):
        reasons.append('需要先导入新字幕，再使用最新文本生成总结')
    return {'approved': not reasons, 'reasons': reasons or ['动作符合视频类型、依赖顺序和处理范围；执行前再次核对文件版本']}
