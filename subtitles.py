import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode, urlparse

import requests

# ==========================================
EMBY_SERVERS = [
    {
        "name": "",
        "url": "",
        "username": "",
        "password": "",
    },
    {
        "name": "",
        "url": "",
        "username": "",
        "password": "",
    }
]

# 兼容旧版单服务器配置；如果 EMBY_SERVERS 已填好，也可以保留为空。
EMBY_URL = ""  # 你的 Emby 服务器地址，不要以斜杠结尾
USERNAME = ""  # Emby 用户名
PASSWORD = ""  # Emby 密码

OUTPUT_DIR = "D:\\VSCode\\PC\\subtitles"
SUBTITLE_KEYWORDS = ["chi", "cn"]  # 留空表示下载全部文本字幕

ASK_SERIES_MODE_EACH_TIME = True
DEFAULT_SERIES_MODE = "all"  # all | select | incremental
DEFAULT_EPISODE_SELECTION = ""  # 例如: S1E2,S1E3

ASK_MOVIE_MODE_EACH_TIME = True
DEFAULT_MOVIE_MODE = "all"  # all | incremental

DEVICE_ID = str(uuid.uuid4())
# ==========================================

IMAGE_CODECS = {
    "pgssub", "dvdsub", "vobsub", "hdmv_pgs_subtitle", "dvb_subtitle"
}


def sanitize_name(name):
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Unknown"


def normalize_codec(codec):
    codec = (codec or "srt").lower()
    return "srt" if codec == "subrip" else codec


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
    return sanitize_name(label).replace(" ", "_")


def build_subtitle_filename(base_name, stream, codec):
    index = stream.get("Index")
    if isinstance(index, int):
        index_part = f"stream{index:02d}"
    else:
        index_part = "streamNA"
    return f"{base_name}_{index_part}_{get_stream_label(stream)}.{codec}"


def format_episode_key(season_num, ep_num):
    return f"S{season_num:02d}E{ep_num:02d}"


def expand_episode_range(start_season, start_ep, end_season, end_ep):
    if end_season < start_season or (end_season == start_season and end_ep < start_ep):
        raise ValueError("集数范围结束位置不能小于开始位置")

    if start_season != end_season:
        raise ValueError("暂不支持跨季度范围，请按同一季分别输入")

    return {
        (start_season, ep_num)
        for ep_num in range(start_ep, end_ep + 1)
    }


def parse_selection_clause(clause):
    clause = clause.strip()
    if not clause:
        return set()

    normalized = re.sub(r"\s+", " ", clause)

    match = re.fullmatch(r"[sS](\d+)[eE](\d+)", normalized)
    if match:
        return {(int(match.group(1)), int(match.group(2)))}

    match = re.fullmatch(r"(\d+)\s+(\d+)", normalized)
    if match:
        return {(int(match.group(1)), int(match.group(2)))}

    match = re.fullmatch(r"(\d+)[\-xX._](\d+)", normalized)
    if match:
        return {(int(match.group(1)), int(match.group(2)))}

    match = re.fullmatch(r"[sS](\d+)[eE](\d+)\s*[-~～]\s*[sS]?(\d+)[eE](\d+)", normalized)
    if match:
        return expand_episode_range(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
        )

    match = re.fullmatch(r"(\d+)\s+(\d+)\s*[-~～]\s*(\d+)", normalized)
    if match:
        return expand_episode_range(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(1)),
            int(match.group(3)),
        )

    raise ValueError(f"无法识别的集数格式: {clause}")


def parse_episode_selection(selection_text):
    selected = set()
    invalid_tokens = []
    tokens = [token.strip() for token in re.split(r"[,，；;、]+", selection_text) if token.strip()]

    for token in tokens:
        try:
            selected.update(parse_selection_clause(token))
        except ValueError:
            invalid_tokens.append(token)

    if invalid_tokens:
        raise ValueError(
            "无法识别的集数格式: "
            + ", ".join(invalid_tokens)
            + "。支持示例: S1E2, 1 6, 1-6, 1x6, S1E1-S1E3, 1 1-3"
        )
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
    print("[2] 筛选指定集数下载（例如 S1E2, 1 6, 1-6, 1 1-3）")
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
            prompt = "请输入要下载的集数（例如 S1E2, 1 6, 1-6, 1 1-3）"
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
    return (
        f'MediaBrowser Client="Tsukimi", Device="Windows", '
        f'DeviceId="{DEVICE_ID}", Version="v0.0.1"'
    )


def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Dart/3.3 (dart:io)",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "X-Emby-Authorization": get_auth_string(),
    })
    return session


def get_server_name(server, fallback_index=None):
    raw_name = (server.get("name") or "").strip()
    if raw_name:
        return raw_name

    url = (server.get("url") or "").strip()
    if url:
        parsed = urlparse(url)
        if parsed.netloc:
            return parsed.netloc
        return url

    if fallback_index is not None:
        return f"Emby {fallback_index}"
    return "Emby"


def normalize_server_config(server, fallback_index=None):
    normalized = {
        "name": get_server_name(server, fallback_index),
        "url": (server.get("url") or "").strip().rstrip("/"),
        "username": (server.get("username") or "").strip(),
        "password": server.get("password") or "",
    }
    if not normalized["url"] or not normalized["username"] or not normalized["password"]:
        return None
    return normalized


def get_configured_servers():
    servers = []
    seen = set()

    for index, server in enumerate(EMBY_SERVERS, start=1):
        normalized = normalize_server_config(server, index)
        if not normalized:
            continue
        key = (normalized["url"], normalized["username"])
        if key in seen:
            continue
        seen.add(key)
        servers.append(normalized)

    legacy_server = normalize_server_config({
        "name": "default",
        "url": EMBY_URL,
        "username": USERNAME,
        "password": PASSWORD,
    })
    if legacy_server:
        key = (legacy_server["url"], legacy_server["username"])
        if key not in seen:
            servers.append(legacy_server)

    return servers


def login_to_server(server):
    session = create_session()
    server_name = server["name"]
    print(f"正在尝试登录 [{server_name}] 用户: {server['username']}...")

    url = f"{server['url']}/Users/AuthenticateByName"
    payload = {"Username": server["username"], "Pw": server["password"]}
    try:
        response = session.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"[{server_name}] 登录失败，状态码: {response.status_code}")
            return None

        data = response.json()
        session.headers.update({"X-Emby-Token": data["AccessToken"]})
        print(f"[{server_name}] 登录成功！")
        return {
            "name": server_name,
            "url": server["url"],
            "username": server["username"],
            "session": session,
            "token": data["AccessToken"],
            "user_id": data["User"]["Id"],
        }
    except requests.exceptions.RequestException as exc:
        print(f"[{server_name}] 登录请求发生错误: {exc}")
        return None


def initialize_clients():
    servers = get_configured_servers()
    if not servers:
        print("未配置可用的 Emby 服务器，请先填写 EMBY_SERVERS 或旧版单服务器配置。")
        return []

    clients = [None] * len(servers)
    print(f"正在并行登录 {len(servers)} 个 Emby...")

    with ThreadPoolExecutor(max_workers=max(1, len(servers))) as executor:
        futures = {
            executor.submit(login_to_server, server): index
            for index, server in enumerate(servers)
        }
        for future in as_completed(futures):
            index = futures[future]
            server_name = servers[index]["name"]
            try:
                client = future.result()
            except Exception as exc:
                print(f"[{server_name}] 登录时发生未预期错误: {exc}")
                continue

            if client:
                clients[index] = client

    return [client for client in clients if client]


def search_items(client, search_term):
    url = f"{client['url']}/Users/{client['user_id']}/Items"
    params = {
        "SearchTerm": search_term,
        "IncludeItemTypes": "Series,Movie",
        "Recursive": "true",
        "Limit": 15,
    }
    response = client["session"].get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json().get("Items", [])


def interactive_search(clients):
    search_term = input("请输入你想搜索的影视名称 (输入 q 退出): ").strip()
    if search_term.lower() == "q":
        raise SystemExit(0)

    print(f"\n正在同时搜索 {len(clients)} 个 Emby: {search_term}...")
    results_by_server = {client["name"]: [] for client in clients}

    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as executor:
        futures = {executor.submit(search_items, client, search_term): client for client in clients}
        for future in as_completed(futures):
            client = futures[future]
            try:
                items = future.result()
                results_by_server[client["name"]] = items
                print(f"[{client['name']}] 搜索完成，找到 {len(items)} 个匹配项。")
            except requests.exceptions.RequestException as exc:
                print(f"[{client['name']}] 搜索请求失败: {exc}")
            except Exception as exc:
                print(f"[{client['name']}] 搜索时发生未预期错误: {exc}")

    merged_results = []
    # 展示顺序按 EMBY_SERVERS 配置从上到下，便于控制来源优先级。
    for client in clients:
        for item in results_by_server.get(client["name"], []):
            merged_results.append({
                "client": client,
                "item": item,
            })

    if not merged_results:
        print("所有 Emby 都未找到匹配的影视，请换个关键词重试。\n")
        return None

    print("找到以下匹配项：")
    for index, result in enumerate(merged_results, start=1):
        item = result["item"]
        client = result["client"]
        year = item.get("ProductionYear", "未知年份")
        print(f"[{index}] [{client['name']}] {item['Name']} ({year}) - 类型: {item['Type']}")

    while True:
        try:
            choice = input(f"\n请选择对应的序号 (1-{len(merged_results)}，输入 0 取消): ").strip()
            if choice == "0":
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(merged_results):
                selected = merged_results[idx]
                item = selected["item"]
                client = selected["client"]
                print(f"\n你选择了: [{client['name']}] {item['Name']}\n")
                return selected
            print("序号超出范围，请重新输入。")
        except ValueError:
            print("请输入有效的数字序号。")


def get_episodes(client, series_id):
    url = f"{client['url']}/Shows/{series_id}/Episodes"
    params = {"UserId": client["user_id"], "Fields": "MediaSources"}
    try:
        response = client["session"].get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json().get("Items", [])
    except requests.exceptions.RequestException as exc:
        print(f"[{client['name']}] 获取剧集失败: {exc}")
        return []


def get_movie_details(client, movie_id):
    url = f"{client['url']}/Users/{client['user_id']}/Items/{movie_id}"
    params = {"Fields": "MediaSources"}
    try:
        response = client["session"].get(url, params=params, timeout=15)
        response.raise_for_status()
        return [response.json()]
    except requests.exceptions.RequestException as exc:
        print(f"[{client['name']}] 获取电影详情失败: {exc}")
        return []


def register_play_session(client, item_id, source_id):
    play_session_id = str(uuid.uuid4()).replace("-", "")
    url = f"{client['url']}/Sessions/Playing"
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
    params = {"api_key": client["token"]}
    try:
        res = client["session"].post(url, json=payload, params=params, timeout=10)
        if res.status_code in (200, 204):
            print(f"    [{client['name']}] [会话] 注册成功: {play_session_id[:8]}...")
        else:
            print(f"    [{client['name']}] [会话] 注册返回 {res.status_code}，仍将尝试下载")
    except requests.exceptions.RequestException as exc:
        print(f"    [{client['name']}] [会话] 注册请求失败: {exc}，仍将尝试下载")
    return play_session_id


def probe_video_stream(client, item_id, source_id, play_session_id):
    """
    用 Range 请求只取视频流头部 32KB，让服务端打开媒体文件。
    这是触发服务端 demux 上下文的关键，内嵌字幕必须在此之后才能提取。
    """
    video_url = f"{client['url']}/Videos/{item_id}/stream"
    params = {
        "api_key": client["token"],
        "DeviceId": DEVICE_ID,
        "PlaySessionId": play_session_id,
        "MediaSourceId": source_id,
        "Static": "true",
    }
    headers = {"Range": "bytes=0-32767"}
    print(f"    [{client['name']}] [探测] 请求视频流头部以触发服务端文件打开...", end="", flush=True)
    try:
        res = client["session"].get(video_url, params=params, headers=headers, timeout=20, stream=True)
        res.close()
        if res.status_code in (200, 206):
            print(f" 成功 (HTTP {res.status_code})")
        else:
            print(f" 返回 {res.status_code}，将继续尝试字幕请求")
    except requests.exceptions.Timeout:
        print(" 探测超时，将继续尝试字幕请求")
    except requests.exceptions.RequestException as exc:
        print(f" 探测失败: {exc}，将继续尝试字幕请求")

    time.sleep(1)


def stop_play_session(client, item_id, play_session_id):
    url = f"{client['url']}/Sessions/Playing/Stopped"
    payload = {
        "ItemId": item_id,
        "PlaySessionId": play_session_id,
        "PositionTicks": 0,
    }
    params = {"api_key": client["token"]}
    try:
        client["session"].post(url, json=payload, params=params, timeout=5)
        print(f"    [{client['name']}] [会话] 已关闭: {play_session_id[:8]}...")
    except requests.exceptions.RequestException:
        pass


def download_subtitles_for_items(client, items, target_output_dir, download_mode):
    ensure_directory(OUTPUT_DIR)
    ensure_directory(target_output_dir)
    keywords = get_configured_keywords()

    print(f"当前下载来源: {client['name']} ({client['url']})")
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
            stream for stream in streams
            if stream.get("Type") == "Subtitle"
            and (stream.get("Codec") or "").lower() not in IMAGE_CODECS
        ]
        if not sub_streams:
            print(f"[{base_name}] 没有可提取的文本字幕，跳过。")
            continue

        sub_streams = [stream for stream in sub_streams if stream_matches_keywords(stream, keywords)]
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

        play_session_id = register_play_session(client, item_id, source_id)
        probe_video_stream(client, item_id, source_id, play_session_id)

        sub_downloaded = 0
        try:
            for stream, index, codec, filename, filepath in pending_streams:
                lang = stream.get("Language", "und")
                is_external = stream.get("IsExternal", False)
                stream_title = stream.get("DisplayTitle") or stream.get("Title")

                sub_url = (
                    f"{client['url']}/Videos/{item_id}/{source_id}"
                    f"/Subtitles/{index}/Stream.{codec}"
                )
                req_params = {
                    "api_key": client["token"],
                    "DeviceId": DEVICE_ID,
                    "PlaySessionId": play_session_id,
                }

                ext_str = "外挂" if is_external else "内嵌"
                detail_parts = [ext_str, lang, codec]
                if stream_title:
                    detail_parts.append(stream_title)

                print(f"[{base_name}] 请求流 {index} ({', '.join(detail_parts)})...", end="", flush=True)

                full_debug_url = f"{sub_url}?{urlencode(req_params)}"
                curl_cmd = (
                    f'curl -H "User-Agent: Dart/3.3 (dart:io)" '
                    f'-H "X-Emby-Authorization: {get_auth_string()}" '
                    f'"{full_debug_url}" -o test_sub.{codec}'
                )

                success = False
                for attempt in range(3):
                    if attempt > 0:
                        print(f"    [重试] 第 {attempt} 次重试，等待 1 秒...", end="", flush=True)
                        time.sleep(1)
                    try:
                        res = client["session"].get(sub_url, params=req_params, timeout=45)
                        if res.status_code == 200:
                            sub_downloaded += 1
                            with open(filepath, "wb") as file_obj:
                                file_obj.write(res.content)
                            print(f" 成功! 保存为 {filename}")
                            success = True
                            break
                        print(f" 失败! 状态码: {res.status_code}")
                    except requests.exceptions.Timeout:
                        print(" 超时!")
                    except requests.exceptions.RequestException as exc:
                        print(f" 网络错误: {exc}")

                if not success:
                    print("    -> 已重试 2 次仍失败，放弃该字幕流")
                    print(f"    -> 调试链接: {full_debug_url}")
                    print(f"    -> 终端测试命令: {curl_cmd}")

                time.sleep(0.5)
        finally:
            stop_play_session(client, item_id, play_session_id)

        if sub_downloaded == 0:
            print(f"[{base_name}] 该媒体源中没有成功提取到文本字幕。")

        if i < len(items) - 1:
            print("    [等待] 3 秒后处理下一集...")
            time.sleep(3)


def main():
    clients = initialize_clients()
    if not clients:
        return

    print()
    while True:
        selected = interactive_search(clients)
        if not selected:
            continue

        client = selected["client"]
        target = selected["item"]
        item_id = target["Id"]
        item_type = target["Type"]
        target_output_dir = os.path.join(OUTPUT_DIR, get_target_folder_name(target))
        ensure_directory(target_output_dir)

        print(f"已选择来源: {client['name']} ({client['url']})")
        print(f"字幕保存目录: {target_output_dir}")

        if item_type == "Series":
            episodes = get_episodes(client, item_id)
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
            download_subtitles_for_items(client, episodes, target_output_dir, mode)
        elif item_type == "Movie":
            mode = prompt_movie_download_mode()
            movies = get_movie_details(client, item_id)
            print("识别为电影，开始提取字幕...")
            download_subtitles_for_items(client, movies, target_output_dir, mode)

        print("\n--- 提取完毕 ---")


if __name__ == "__main__":
    main()
