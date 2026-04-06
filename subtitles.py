import os
import re
import uuid
import time
from urllib.parse import urlencode

import requests

# ==========================================
EMBY_URL = ""  # 你的 Emby 服务器地址，不要以斜杠结尾
USERNAME = ""  # Emby 用户名
PASSWORD = ""  # Emby 密码
OUTPUT_DIR = "./subtitles"
SUBTITLE_KEYWORDS = ["chi", "cn"]  # 留空表示下载全部文本字幕

ASK_SERIES_MODE_EACH_TIME = True
DEFAULT_SERIES_MODE = "all"  # all | select | incremental
DEFAULT_EPISODE_SELECTION = ""  # 例如: S1E2,S1E3

ASK_MOVIE_MODE_EACH_TIME = True
DEFAULT_MOVIE_MODE = "all"  # all | incremental

DEVICE_ID = str(uuid.uuid4())
# ==========================================

IMAGE_CODECS = {
    'pgssub', 'dvdsub', 'vobsub', 'hdmv_pgs_subtitle', 'dvb_subtitle'
}


def sanitize_name(name):
    cleaned = re.sub(r'[<>:"/\\|?*]+', '_', (name or '').strip())
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
    return cleaned or 'Unknown'


def normalize_codec(codec):
    codec = (codec or 'srt').lower()
    return 'srt' if codec == 'subrip' else codec


def get_configured_keywords():
    return [keyword.strip().lower() for keyword in SUBTITLE_KEYWORDS if keyword.strip()]


def get_item_base_name(item):
    if item["Type"] == "Episode":
        season_num = item.get("ParentIndexNumber") or 0
        ep_num = item.get("IndexNumber") or 0
        return f"S{season_num:02d}E{ep_num:02d}"
    return sanitize_name(item.get("Name", "Movie"))


def get_target_folder_name(target):
    name = target.get("Name", "Unknown")
    year = target.get("ProductionYear")
    if year:
        name = f"{name} ({year})"
    return sanitize_name(name)


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)


def stream_matches_keywords(stream, keywords):
    if not keywords:
        return True

    search_parts = [
        stream.get("Language"),
        stream.get("Title"),
        stream.get("DisplayTitle"),
        stream.get("Codec"),
        stream.get("Path"),
    ]
    search_text = " ".join(str(part).lower() for part in search_parts if part)
    return any(keyword in search_text for keyword in keywords)


def get_stream_label(stream):
    label = stream.get("DisplayTitle") or stream.get("Title") or stream.get("Language") or "subtitle"
    return sanitize_name(label).replace(' ', '_')


def build_subtitle_filename(base_name, stream, codec):
    index = stream.get("Index")
    if isinstance(index, int):
        index_part = f"stream{index:02d}"
    else:
        index_part = "streamNA"
    return f"{base_name}_{index_part}_{get_stream_label(stream)}.{codec}"


def format_episode_key(season_num, ep_num):
    return f"S{season_num:02d}E{ep_num:02d}"


def parse_episode_selection(selection_text):
    selected = set()
    invalid_tokens = []
    tokens = [token.strip() for token in re.split(r"[\s,，]+", selection_text) if token.strip()]

    for token in tokens:
        match = re.fullmatch(r"[sS](\d+)[eE](\d+)", token)
        if not match:
            invalid_tokens.append(token)
            continue
        selected.add((int(match.group(1)), int(match.group(2))))

    if invalid_tokens:
        raise ValueError(f"无法识别的集数格式: {', '.join(invalid_tokens)}")
    if not selected:
        raise ValueError("未提供有效的集数筛选")
    return selected


def filter_series_items_by_selection(items, selection_text):
    selected_pairs = parse_episode_selection(selection_text)
    filtered_items = []
    matched_pairs = set()

    for item in items:
        pair = (item.get("ParentIndexNumber") or 0, item.get("IndexNumber") or 0)
        if pair in selected_pairs:
            filtered_items.append(item)
            matched_pairs.add(pair)

    missing_pairs = selected_pairs - matched_pairs
    if missing_pairs:
        missing_text = ", ".join(
            format_episode_key(season_num, ep_num)
            for season_num, ep_num in sorted(missing_pairs)
        )
        print(f"未找到指定集数: {missing_text}")

    return filtered_items


def prompt_series_download_mode():
    if not ASK_SERIES_MODE_EACH_TIME:
        return DEFAULT_SERIES_MODE, DEFAULT_EPISODE_SELECTION

    default_choice = {
        "all": "1",
        "select": "2",
        "incremental": "3",
    }.get(DEFAULT_SERIES_MODE, "1")

    print("下载模式：")
    print("[1] 全部下载")
    print("[2] 筛选指定集数下载（例如 S1E2,S1E3）")
    print("[3] 增量下载（跳过已存在字幕）")

    while True:
        choice = input(f"请选择模式 (默认 {default_choice}): ").strip() or default_choice
        if choice not in {"1", "2", "3"}:
            print("请输入有效的模式序号。")
            continue

        mode = {
            "1": "all",
            "2": "select",
            "3": "incremental",
        }[choice]

        if mode != "select":
            return mode, ""

        while True:
            prompt = "请输入要下载的集数（例如 S1E2,S1E3）"
            if DEFAULT_EPISODE_SELECTION:
                prompt += f"，直接回车使用默认值 {DEFAULT_EPISODE_SELECTION}"
            prompt += ": "

            selection = input(prompt).strip() or DEFAULT_EPISODE_SELECTION
            if not selection:
                print("指定集数模式必须输入至少一个集数。")
                continue

            try:
                parse_episode_selection(selection)
                return mode, selection
            except ValueError as exc:
                print(exc)


def prompt_movie_download_mode():
    if not ASK_MOVIE_MODE_EACH_TIME:
        return DEFAULT_MOVIE_MODE

    default_choice = {
        "all": "1",
        "incremental": "2",
    }.get(DEFAULT_MOVIE_MODE, "1")

    print("下载模式：")
    print("[1] 全部下载")
    print("[2] 增量下载（跳过已存在字幕）")

    while True:
        choice = input(f"请选择模式 (默认 {default_choice}): ").strip() or default_choice
        if choice == "1":
            return "all"
        if choice == "2":
            return "incremental"
        print("请输入有效的模式序号。")

def get_auth_string():
    return (f'MediaBrowser Client="Tsukimi", Device="Windows", '
            f'DeviceId="{DEVICE_ID}", Version="v0.0.1"')

def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Dart/3.3 (dart:io)",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "X-Emby-Authorization": get_auth_string()
    })
    return session

def login(session):
    print(f"正在尝试登录用户: {USERNAME}...")
    url = f"{EMBY_URL}/Users/AuthenticateByName"
    payload = {"Username": USERNAME, "Pw": PASSWORD}
    try:
        response = session.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            print("登录成功！\n")
            session.headers.update({"X-Emby-Token": data["AccessToken"]})
            return data["AccessToken"], data["User"]["Id"]
        else:
            print(f"登录失败，状态码: {response.status_code}")
            exit(1)
    except requests.exceptions.RequestException as e:
        print(f"登录请求发生错误: {e}")
        exit(1)

def register_play_session(session, token, item_id, source_id):
    play_session_id = str(uuid.uuid4()).replace("-", "")
    url = f"{EMBY_URL}/Sessions/Playing"
    payload = {
        "ItemId": item_id,
        "MediaSourceId": source_id,
        "PlaySessionId": play_session_id,
        "PlayMethod": "DirectStream",
        "CanSeek": True,
        "IsPaused": False,
        "IsMuted": False,
        "PositionTicks": 0,
        "AudioStreamIndex": 1,
        "SubtitleStreamIndex": -1,
    }
    params = {"api_key": token}
    try:
        res = session.post(url, json=payload, params=params, timeout=10)
        if res.status_code in (200, 204):
            print(f"    [会话] 注册成功: {play_session_id[:8]}...")
        else:
            print(f"    [会话] 注册返回 {res.status_code}，仍将尝试下载")
    except requests.exceptions.RequestException as e:
        print(f"    [会话] 注册请求失败: {e}，仍将尝试下载")
    return play_session_id

def probe_video_stream(session, token, item_id, source_id, play_session_id):
    """
    用 Range 请求只取视频流头部 32KB，让服务端打开媒体文件。
    这是触发服务端 demux 上下文的关键，内嵌字幕必须在此之后才能提取。
    """
    video_url = f"{EMBY_URL}/Videos/{item_id}/stream"
    params = {
        "api_key": token,
        "DeviceId": DEVICE_ID,
        "PlaySessionId": play_session_id,
        "MediaSourceId": source_id,
        "Static": "true",
    }
    headers = {"Range": "bytes=0-32767"}
    print(f"    [探测] 请求视频流头部以触发服务端文件打开...", end="", flush=True)
    try:
        res = session.get(video_url, params=params, headers=headers,
                          timeout=20, stream=True)
        res.close()
        if res.status_code in (200, 206):
            print(f" 成功 (HTTP {res.status_code})")
        else:
            print(f" 返回 {res.status_code}，将继续尝试字幕请求")
    except requests.exceptions.Timeout:
        print(" 探测超时，将继续尝试字幕请求")
    except requests.exceptions.RequestException as e:
        print(f" 探测失败: {e}，将继续尝试字幕请求")

    time.sleep(1)

def stop_play_session(session, token, item_id, play_session_id):
    url = f"{EMBY_URL}/Sessions/Playing/Stopped"
    payload = {
        "ItemId": item_id,
        "PlaySessionId": play_session_id,
        "PositionTicks": 0,
    }
    params = {"api_key": token}
    try:
        session.post(url, json=payload, params=params, timeout=5)
        print(f"    [会话] 已关闭: {play_session_id[:8]}...")
    except Exception:
        pass

def interactive_search(session, user_id):
    search_term = input("请输入你想搜索的影视名称 (输入 q 退出): ").strip()
    if search_term.lower() == 'q':
        exit(0)

    print(f"\n正在搜索: {search_term}...")
    url = f"{EMBY_URL}/Users/{user_id}/Items"
    params = {
        "SearchTerm": search_term,
        "IncludeItemTypes": "Series,Movie",
        "Recursive": "true",
        "Limit": 15
    }
    try:
        response = session.get(url, params=params, timeout=15)
        items = response.json().get("Items", [])
    except requests.exceptions.RequestException as e:
        print(f"搜索请求失败: {e}\n")
        return None, None

    if not items:
        print("未找到匹配的影视，请换个关键词重试。\n")
        return None, None

    print("找到以下匹配项：")
    for i, item in enumerate(items):
        year = item.get("ProductionYear", "未知年份")
        print(f"[{i + 1}] {item['Name']} ({year}) - 类型: {item['Type']}")

    while True:
        try:
            choice = input(f"\n请选择对应的序号 (1-{len(items)}，输入 0 取消): ").strip()
            if choice == '0':
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                target = items[idx]
                print(f"\n你选择了: {target['Name']}\n")
                return target
            else:
                print("序号超出范围，请重新输入。")
        except ValueError:
            print("请输入有效的数字序号。")

def get_episodes(session, user_id, series_id):
    url = f"{EMBY_URL}/Shows/{series_id}/Episodes"
    params = {"UserId": user_id, "Fields": "MediaSources"}
    try:
        response = session.get(url, params=params, timeout=15)
        return response.json().get("Items", [])
    except Exception:
        return []

def get_movie_details(session, user_id, movie_id):
    url = f"{EMBY_URL}/Users/{user_id}/Items/{movie_id}"
    params = {"Fields": "MediaSources"}
    try:
        response = session.get(url, params=params, timeout=15)
        return [response.json()]
    except Exception:
        return []

def download_subtitles_for_items(session, token, items, target_output_dir, download_mode):
    ensure_directory(OUTPUT_DIR)
    ensure_directory(target_output_dir)
    keywords = get_configured_keywords()

    if keywords:
        print(f"字幕关键词过滤已启用: {', '.join(keywords)}")
    else:
        print("字幕关键词过滤未启用，将下载全部文本字幕。")

    for i, item in enumerate(items):
        item_id = item["Id"]
        base_name = get_item_base_name(item)

        sources = item.get("MediaSources", [])
        if not sources:
            print(f"[{base_name}] 未找到媒体源。")
            continue

        source = sources[0]
        source_id = source["Id"]
        streams = source.get("MediaStreams", [])

        sub_streams = [
            s for s in streams
            if s.get("Type") == "Subtitle"
            and (s.get("Codec") or "").lower() not in IMAGE_CODECS
        ]
        if not sub_streams:
            print(f"[{base_name}] 没有可提取的文本字幕，跳过。")
            continue

        sub_streams = [s for s in sub_streams if stream_matches_keywords(s, keywords)]
        if not sub_streams:
            print(f"[{base_name}] 没有匹配关键词的字幕流，跳过。")
            continue

        pending_streams = []
        for stream in sub_streams:
            index = stream.get("Index")
            if index is None:
                print(f"[{base_name}] 字幕流缺少索引，跳过。")
                continue

            codec = normalize_codec(stream.get("Codec"))
            filename = build_subtitle_filename(base_name, stream, codec)
            filepath = os.path.join(target_output_dir, filename)

            if download_mode == "incremental" and os.path.exists(filepath):
                print(f"[{base_name}] 已存在，跳过 {filename}")
                continue

            pending_streams.append((stream, index, codec, filename, filepath))

        if not pending_streams:
            if download_mode == "incremental":
                print(f"[{base_name}] 增量模式下已存在所有匹配字幕，跳过。")
            else:
                print(f"[{base_name}] 没有需要下载的字幕流。")
            continue

        # 1. 注册播放会话
        play_session_id = register_play_session(session, token, item_id, source_id)

        # 2. 探测视频流头部，触发服务端打开媒体文件 demux 上下文
        probe_video_stream(session, token, item_id, source_id, play_session_id)

        sub_downloaded = 0
        try:
            for stream, index, codec, filename, filepath in pending_streams:
                lang = stream.get("Language", "und")
                is_external = stream.get("IsExternal", False)
                stream_title = stream.get("DisplayTitle") or stream.get("Title")

                sub_url = (f"{EMBY_URL}/Videos/{item_id}/{source_id}"
                           f"/Subtitles/{index}/Stream.{codec}")
                req_params = {
                    "api_key": token,
                    "DeviceId": DEVICE_ID,
                    "PlaySessionId": play_session_id,
                }

                ext_str = "外挂" if is_external else "内嵌"
                detail_parts = [ext_str, lang, codec]
                if stream_title:
                    detail_parts.append(stream_title)

                print(f"[{base_name}] 请求流 {index} ({', '.join(detail_parts)})...",
                      end="", flush=True)

                full_debug_url = f"{sub_url}?{urlencode(req_params)}"
                curl_cmd = (
                    f'curl -H "User-Agent: Dart/3.3 (dart:io)" '
                    f'-H "X-Emby-Authorization: {get_auth_string()}" '
                    f'"{full_debug_url}" -o test_sub.{codec}'
                )

                success = False
                for attempt in range(3):  # 首次 + 最多重试 2 次
                    if attempt > 0:
                        print(f"    [重试] 第 {attempt} 次重试，等待 1 秒...",
                              end="", flush=True)
                        time.sleep(1)
                    try:
                        res = session.get(sub_url, params=req_params, timeout=45)
                        if res.status_code == 200:
                            sub_downloaded += 1
                            with open(filepath, "wb") as f:
                                f.write(res.content)
                            print(f" 成功! 保存为 {filename}")
                            success = True
                            break
                        else:
                            print(f" 失败! 状态码: {res.status_code}")
                    except requests.exceptions.Timeout:
                        print(" 超时!")
                    except requests.exceptions.RequestException as e:
                        print(f" 网络错误: {e}")

                if not success:
                    print(f"    -> 已重试 2 次仍失败，放弃该字幕流")
                    print(f"    -> 调试链接: {full_debug_url}")
                    print(f"    -> 终端测试命令: {curl_cmd}")

                # 每条字幕流下载后间隔 0.5 秒
                time.sleep(0.5)

        finally:
            # 3. 所有字幕流处理完后才关闭会话
            stop_play_session(session, token, item_id, play_session_id)

        if sub_downloaded == 0:
            print(f"[{base_name}] 该媒体源中没有成功提取到文本字幕。")

        # 4. 最后一集不需要等待
        if i < len(items) - 1:
            print(f"    [等待] 3 秒后处理下一集...")
            time.sleep(3)

def main():
    session = create_session()
    token, user_id = login(session)

    while True:
        target = interactive_search(session, user_id)
        if not target:
            continue

        item_id = target["Id"]
        item_type = target["Type"]
        target_output_dir = os.path.join(OUTPUT_DIR, get_target_folder_name(target))
        ensure_directory(target_output_dir)
        print(f"字幕保存目录: {target_output_dir}")

        if item_type == "Series":
            episodes = get_episodes(session, user_id, item_id)
            mode, selection = prompt_series_download_mode()
            if mode == "select":
                try:
                    episodes = filter_series_items_by_selection(episodes, selection)
                except ValueError as exc:
                    print(exc)
                    continue

            if not episodes:
                print("没有符合条件的剧集，返回搜索。")
                continue

            print(f"共找到 {len(episodes)} 集，开始提取字幕...")
            download_subtitles_for_items(session, token, episodes, target_output_dir, mode)
        elif item_type == "Movie":
            mode = prompt_movie_download_mode()
            movies = get_movie_details(session, user_id, item_id)
            print("识别为电影，开始提取字幕...")
            download_subtitles_for_items(session, token, movies, target_output_dir, mode)

        print("\n--- 提取完毕 ---")

if __name__ == "__main__":
    main()
