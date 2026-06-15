"""数据质量检查工具 — 验证爬取结果的 JSON 文件
用法: python check_data.py <json_file>
"""
import json, sys
from collections import Counter

with open(sys.argv[1], 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f'总条数: {len(data)}')
ids = [v['video_id'] for v in data]
unique = len(set(ids))
print(f'唯一 ID 数: {unique}')
if len(data) > unique:
    dup_counts = Counter(ids)
    dups = [(k,v) for k,v in dup_counts.items() if v > 1]
    print(f'重复 ID 数: {len(dups)}, 重复条数: {sum(v-1 for v in dup_counts.values() if v > 1)}')
    print(f'重复示例: {dups[:3]}')

likes = [v['like_count'] for v in data]
comments = [v['comment_count'] for v in data]
shares = [v['share_count'] for v in data]
views = [v['view_count'] for v in data]

print(f'\n点赞: {min(likes)} ~ {max(likes)}')
print(f'评论: {min(comments)} ~ {max(comments)}')
print(f'分享: {min(shares)} ~ {max(shares)}')
print(f'播放量(web端均为0): {set(views)}')
print(f'\n第1条标题: {data[0]["title"][:80]}')
print(f'封面URL长度: {len(data[0]["cover_url"])} 字符')
print(f'视频URL长度: {len(data[0]["video_url"])} 字符')
