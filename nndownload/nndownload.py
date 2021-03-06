#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Download videos and process other links from Niconico (nicovideo.jp)."""

from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import requests

from itertools import tee
import argparse
import asyncio
import collections
import getpass
import json
import logging
import math
import mimetypes
import netrc
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import websockets
import xml.dom.minidom

__version__ = "1.4"
__author__ = "Alex Aplin"
__copyright__ = "Copyright 2019 Alex Aplin"
__license__ = "MIT"

HOST = "nicovideo.jp"

LOGIN_URL = "https://account.nicovideo.jp/api/v1/login?site=niconico"
VIDEO_URL = "https://nicovideo.jp/watch/{0}"
NAMA_URL = "https://live.nicovideo.jp/watch/{0}"
USER_VIDEOS_URL = "https://nicovideo.jp/user/{0}/video?page={1}"
SEIGA_IMAGE_URL = "http://seiga.nicovideo.jp/seiga/{0}"
SEIGA_MANGA_URL = "http://seiga.nicovideo.jp/comic/{0}"
SEIGA_CHAPTER_URL = "http://seiga.nicovideo.jp/watch/{0}"
SEIGA_SOURCE_URL = "http://seiga.nicovideo.jp/image/source/{0}"
SEIGA_CDN_URL = "https://lohas.nicoseiga.jp/"

VIDEO_URL_RE = re.compile(r"(?:https?://(?:(?:(sp|www|seiga)\.)?(?:(live[0-9]?|cas)\.)?(?:(?:nicovideo\.jp/(watch|mylist|user|comic|seiga))|nico\.ms)/))(?:(?:[0-9]+)/)?((?:[a-z]{2})?[0-9]+)")
M3U8_STREAM_RE = re.compile(r"(?:(?:#EXT-X-STREAM-INF)|#EXT-X-I-FRAME-STREAM-INF):.*(?:BANDWIDTH=(\d+)).*\n(.*)")
SEIGA_DRM_KEY_RE = re.compile(r"/image/([a-z0-9]+)")
SEIGA_USER_ID_RE = re.compile(r"user_id=(\d+)")

THUMB_INFO_API = "http://ext.nicovideo.jp/api/getthumbinfo/{0}"
MYLIST_API = "http://flapi.nicovideo.jp/api/getplaylist/mylist/{0}"
COMMENTS_API = "http://nmsg.nicovideo.jp/api"
COMMENTS_POST_JP = "<packet><thread thread=\"{0}\" version=\"20061206\" res_from=\"-1000\" scores=\"1\"/></packet>"
COMMENTS_POST_EN = "<packet><thread thread=\"{0}\" version=\"20061206\" res_from=\"-1000\" language=\"1\" scores=\"1\"/></packet>"

NAMA_HEARTBEAT_INTERVAL_S = 15
DMC_HEARTBEAT_INTERVAL_S = 15
KILOBYTE = 1024
BLOCK_SIZE = 1024
EPSILON = 0.0001
RETRY_ATTEMPTS = 5
BACKOFF_FACTOR = 2  # retry_timeout_s = BACK_OFF_FACTOR * (2 ** ({RETRY_ATTEMPTS} - 1))


MIMETYPES = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png"
}

HTML5_COOKIE = {
    "watch_flash": "0"
}

FLASH_COOKIE = {
    "watch_flash": "1"
}

EN_COOKIE = {
    "lang": "en-us"
}

NAMA_PERMIT_FRAME = json.loads("""
{
    "type": "watch",
    "body": {
        "command": "getpermit",
        "requirement": {
            "broadcastId": "-1",
            "route": "",
            "stream": {
                "protocol": "hls",
                "requireNewStream": true,
                "priorStreamQuality": "abr",
                "isLowLatency": true,
                "isChasePlay": false
            },
            "room": {
                "isCommentable": true,
                "protocol": "webSocket"
            }
        }
    }
}
""")

NAMA_WATCHING_FRAME = json.loads("""
{
    "type": "watch",
    "body": {
        "command": "watching",
        "params": [
            "BROADCAST_ID",
            "-1",
            "0"
        ]
    }
}
""")

PONG_FRAME = json.loads("""{"type":"pong","body":{}}""")

logger = logging.getLogger(__name__)

cmdl_usage = "%(prog)s [options] input"
cmdl_version = __version__
cmdl_parser = argparse.ArgumentParser(usage=cmdl_usage, conflict_handler="resolve")

cmdl_parser.add_argument("-u", "--username", dest="username", metavar="USERNAME", help="account username")
cmdl_parser.add_argument("-p", "--password", dest="password", metavar="PASSWORD", help="account password")
cmdl_parser.add_argument("-n", "--netrc", action="store_true", dest="netrc", help="use .netrc authentication")
cmdl_parser.add_argument("-q", "--quiet", action="store_true", dest="quiet", help="suppress output to console")
cmdl_parser.add_argument("-l", "--log", action="store_true", dest="log", help="log output to file")
cmdl_parser.add_argument("-v", "--version", action="version", version=cmdl_version)
cmdl_parser.add_argument("input", help="URL or file")

dl_group = cmdl_parser.add_argument_group("download options")
dl_group.add_argument("-y", "--proxy", dest="proxy", metavar="PROXY", help="http or socks proxy")
dl_group.add_argument("-o", "--output-path", dest="output_path", metavar="TEMPLATE", help="custom output path (see template options)")
dl_group.add_argument("-r", "--threads", dest="threads", metavar="N", help="download using a specified number of threads")
dl_group.add_argument("-g", "--no-login", action="store_true", dest="no_login", help="create a download session without logging in")
dl_group.add_argument("-f", "--force-high-quality", action="store_true", dest="force_high_quality", help="only download if the high quality source is available")
dl_group.add_argument("-m", "--dump-metadata", action="store_true", dest="dump_metadata", help="dump video metadata to file")
dl_group.add_argument("-t", "--download-thumbnail", action="store_true", dest="download_thumbnail", help="download video thumbnail")
dl_group.add_argument("-c", "--download-comments", action="store_true", dest="download_comments", help="download video comments")
dl_group.add_argument("-e", "--english", action="store_true", dest="download_english", help="request video on english site")
dl_group.add_argument("-aq", "--audio-quality", dest="audio_quality", help="specify audio quality (DMC videos only)")
dl_group.add_argument("-vq", "--video-quality", dest="video_quality", help="specify video quality (DMC videos only)")
dl_group.add_argument("-s", "--skip-media", action="store_true", dest="skip_media", help="skip downloading media")


class AuthenticationException(Exception):
    """Raised when logging in to Niconico failed."""
    pass


class ArgumentException(Exception):
    """Raised when reading the argument failed."""
    pass


class FormatNotSupportedException(Exception):
    """Raised when the response format is not supported."""
    pass


class FormatNotAvailableException(Exception):
    """Raised when the requested format is not available."""
    pass


class ParameterExtractionException(Exception):
    """Raised when parameters could not be successfully extracted."""
    pass


## Utility methods

def configure_logger():
    """Initialize logger."""

    if cmdl_opts.log:
        logger.setLevel(logging.INFO)
        log_handler = logging.FileHandler("[{0}] {1}.log".format("nndownload", time.strftime("%Y-%m-%d")), encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        log_handler.setFormatter(formatter)
        logger.addHandler(log_handler)


def log_exception(error):
    """Process exception for logger."""

    if cmdl_opts.log:
        logger.exception("{0}: {1}\n".format(type(error).__name__, str(error)))


def output(string, level=logging.INFO, force=False):
    """Print status to console unless quiet flag is set."""

    global cmdl_opts
    if cmdl_opts.log:
        logger.log(level, string.strip("\n"))

    if not cmdl_opts.quiet or force:
        sys.stdout.write(string)
        sys.stdout.flush()


def pairwise(iterable):
    """Helper method to pair RTMP URL with stream label."""

    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


def format_bytes(number_bytes):
    """Attach suffix (e.g. 10 T) to number of bytes."""

    try:
        exponent = int(math.log(number_bytes, KILOBYTE))
        suffix = "\0KMGTPE"[exponent]

        if exponent == 0:
            return "{0}{1}".format(number_bytes, suffix)

        converted = float(number_bytes / KILOBYTE ** exponent)
        return "{0:.2f}{1}B".format(converted, suffix)

    except IndexError:
        raise IndexError("Could not format number of bytes")


def calculate_speed(start, now, bytes):
    """Calculate speed based on difference between start and current block call."""

    dif = now - start
    if bytes == 0 or dif < EPSILON:
        return "N/A B"
    return format_bytes(bytes / dif)


def replace_extension(filename, new_extension):
    """Replace the extension in a file path."""

    base_path, _ = os.path.splitext(filename)
    return "{0}.{1}".format(base_path, new_extension)


def sanitize_for_path(value, replace=' '):
    """Remove potentially illegal characters from a path."""

    return re.sub(r'[<>\"\?\\\/\*:]', replace, value)


def create_filename(template_params, is_comic=False):
    """Create filename from document parameters."""

    filename_template = cmdl_opts.output_path

    if filename_template:
        template_dict = dict(template_params)
        template_dict = dict((k, sanitize_for_path(str(v))) for k, v in template_dict.items() if v)
        template_dict = collections.defaultdict(lambda: "__NONE__", template_dict)

        filename = filename_template.format_map(template_dict)
        if is_comic:
            os.makedirs(filename, exist_ok=True)
        elif (os.path.dirname(filename) and not os.path.exists(os.path.dirname(filename))) or os.path.exists(os.path.dirname(filename)):
            os.makedirs(os.path.dirname(filename), exist_ok=True)

        return filename

    elif is_comic:
        directory = os.path.join("{0} - {1}".format(template_params["manga_id"], sanitize_for_path(template_params["manga_title"])), "{0} - {1}".format(template_params["id"], sanitize_for_path(template_params["title"])))
        os.makedirs(directory, exist_ok=True)
        return directory

    else:
        filename = "{0} - {1}.{2}".format(template_params["id"], template_params["title"], template_params["ext"])
        return sanitize_for_path(filename)


def read_file(session, file):
    """Read file and process each line as a URL."""

    with open(file) as file:
        content = file.readlines()

    total_lines = len(content)
    for index, line in enumerate(content):
        try:
            output("{0}/{1}\n".format(index + 1, total_lines), logging.INFO)
            url_mo = valid_url(line)
            if url_mo:
                process_url_mo(session, url_mo)
            else:
                raise ArgumentException("Not a valid URL")

        except (FormatNotSupportedException, FormatNotAvailableException, ParameterExtractionException) as error:
            log_exception(error)
            traceback.print_exc()
            continue


def get_playlist_from_m3u8(m3u8_text):
    """Return last playlist from a master.m3u8 file."""

    best_bandwidth, best_stream = -1, None
    matches = M3U8_STREAM_RE.findall(m3u8_text)

    if not matches:
        raise FormatNotAvailableException("Could not retrieve stream playlist from master playlist")

    else:
        for match in matches:
            stream_bandwidth = int(match[0])
            if stream_bandwidth > best_bandwidth:
                best_bandwidth = stream_bandwidth
                best_stream = match[1]

    return best_stream


def find_extension(mimetype):
    """Determine the file extension from the mimetype."""

    return MIMETYPES.get(mimetype) or mimetypes.guess_extension(mimetype, strict=True)


## Nama methods

def generate_stream(session, master_url):
    """Output the highest quality stream URL for a live Nicoanama broadcast."""

    output("Retrieving master playlist...\n", logging.INFO)

    m3u8 = session.get(master_url)
    m3u8.raise_for_status()

    output("Retrieved master playlist.\n", logging.INFO)

    playlist_slug = get_playlist_from_m3u8(m3u8.text)
    stream_url = master_url.rsplit("/", maxsplit=1)[0] + "/" + playlist_slug
    # stream_url = stream_url.replace("https://", "hls://")

    output("Generated stream URL. Please keep this window open to keep the stream active. Press ^C to exit.\n", logging.INFO)
    output("For more instructions on playing this stream, please consult the README.\n", logging.INFO)
    output("{0}\n".format(stream_url), logging.INFO, force=True)


async def perform_nama_heartbeat(websocket, watching_frame):
    """Send a watching frame periodically to keep the stream alive."""

    while True:
        await websocket.send(json.dumps(watching_frame))
        await asyncio.sleep(NAMA_HEARTBEAT_INTERVAL_S)


async def open_nama_websocket(session, uri, broadcast_id, event_loop):
    """Open a WebSocket connection to receive and generate the stream playlist URL."""

    async with websockets.connect(uri) as websocket:
        watching_frame = NAMA_WATCHING_FRAME
        watching_frame["body"]["params"][0] = broadcast_id

        permit_frame = NAMA_PERMIT_FRAME
        permit_frame["body"]["requirement"]["broadcastId"] = broadcast_id
        await websocket.send(json.dumps(permit_frame))

        heartbeat = event_loop.create_task(perform_nama_heartbeat(websocket, watching_frame))

        try:
            while True:
                frame = json.loads(await websocket.recv())
                frame_type = frame["type"]

                # output(f"SERVER: {frame}\n", logging.DEBUG);

                if frame_type == "watch":
                    command = frame["body"]["command"]

                    if command == "statistics":
                        continue

                    elif command == "currentstream":
                        stream_url = frame["body"]["currentStream"]["uri"]
                        generate_stream(session, stream_url)

                elif frame_type == "ping":
                    await websocket.send(json.dumps(PONG_FRAME))

        except websockets.exceptions.ConnectionClosed:
            output("Connection was closed. Exiting...\n", logging.INFO)
            heartbeat.cancel()
            return


def request_nama(session, nama_id):
    """Generate a stream URL for a live Niconama broadcast."""

    response = session.get(NAMA_URL.format(nama_id))
    response.raise_for_status()

    document = BeautifulSoup(response.text, "html.parser")

    if document.find(id="embedded-data"):
        params = json.loads(document.find(id="embedded-data")["data-props"])

        websocket_url = params["site"]["relive"]["webSocketUrl"]
        broadcast_id = params["program"]["broadcastId"]

        event_loop = asyncio.get_event_loop()
        event_loop.run_until_complete(
            open_nama_websocket(session, websocket_url, broadcast_id, event_loop))

    else:
        raise FormatNotAvailableException("Could not retrieve nama info")


## Seiga methods

def decrypt_seiga_drm(enc_bytes, key):
    """Decrypt the light DRM applied to certain Seiga images."""

    n = []
    a = 8

    for i in range(a):
        start = 2 * i
        value = int(key[start:start + 2], 16)
        n.append(value)

    dec_bytes = bytearray(enc_bytes)
    for i in range(len(enc_bytes)):
        dec_bytes[i] = dec_bytes[i] ^ n[i % a]

    return dec_bytes


def determine_seiga_file_type(dec_bytes):
    """Determine the image file type from a bytes array using magic numbers."""

    if 255 == dec_bytes[0] and 216 == dec_bytes[1] and 255 == dec_bytes[len(dec_bytes) - 2] and 217 == dec_bytes[len(dec_bytes) - 1]:
        return "jpg"
    elif 137 == dec_bytes[0] and 80 == dec_bytes[1] and 78 == dec_bytes[2] and 71 == dec_bytes[3]:
        return "png"
    elif 71 == dec_bytes[0] and 73 == dec_bytes[1] and 70 == dec_bytes[2] and 6 == dec_bytes[3]:
        return "gif"
    else:
        raise FormatNotSupportedException("Could not succesffully determine image file type")


def collect_seiga_image_parameters(session, document, template_params):
    """Extract template parameters from a Seiga image page."""

    template_params["id"] = document.select("#ko_cpp")[0]["data-target_id"]
    template_params["title"] = document.select("h1.title")[0].text
    template_params["description"] = document.select("p.discription")[0].text
    template_params["published"] = document.select("span.created")[0].text
    template_params["uploader"] = document.select("li.user_name strong")[0].text
    template_params["uploader_id"] = document.select("li.user_link a")[0]["href"].replace("/user/illust/", "")
    template_params["view_count"] = document.select("li.view span.count_value")[0].text
    template_params["comment_count"] = document.select("li.comment span.count_value")[0].text
    template_params["clip_count"] = document.select("li.clip span.count_value")[0].text

    source_page = session.get(SEIGA_SOURCE_URL.format(template_params["id"].lstrip("im")))
    source_page.raise_for_status()
    source_document = BeautifulSoup(source_page.text, "html.parser")

    source_url_relative = source_document.select("div.illust_view_big")[0]["data-src"]
    template_params["url"] = source_url_relative.replace("/", SEIGA_CDN_URL, 1)

    source_image = session.get(template_params["url"])
    source_image.raise_for_status()
    mimetype = source_image.headers["Content-Type"]
    template_params["ext"] = find_extension(mimetype)

    return template_params


def collect_seiga_manga_parameters(document, template_params):
    """Extract template parameters from a Seiga manga chapter page."""

    template_params["manga_id"] = document.select("#full_watch_head_bar")[0]["data-content-id"]
    template_params["manga_title"] = document.select("div.manga_title a")[0].text
    template_params["id"] = "mg" + document.select("#full_watch_head_bar")[0]["data-theme-id"]
    template_params["page_count"] = document.select("#full_watch_head_bar")[0]["data-page-count"]
    template_params["title"] = document.select("span.episode_title")[0].text
    template_params["published"] = document.select("span.created")[0].text
    template_params["description"] = document.select("div.description .full")[0].text
    template_params["comment_count"] = document.select("#comment_count")[0].text
    template_params["view_count"] = document.select("#view_count")[0].text
    template_params["uploader"] = document.select("span.author_name")[0].text

    # No uploader ID for official manga uploads
    if document.select("dd.user_name a"):
        template_params["uploader_id"] = SEIGA_USER_ID_RE.search(document.select("dd.user_name a")[0]["href"]).group(1)

    return template_params


def download_manga_chapter(session, chapter_id):
    """Download the requested chapter for a Seiga manga."""

    response = session.get(SEIGA_CHAPTER_URL.format(chapter_id))
    response.raise_for_status()

    document = BeautifulSoup(response.text, "html.parser")

    template_params = {}
    template_params = collect_seiga_manga_parameters(document, template_params)
    chapter_directory = create_filename(template_params, is_comic=True)

    if not cmdl_opts.skip_media:
        output("Downloading {0} to \"{1}\"...\n".format(chapter_id, chapter_directory), logging.INFO)

        images = document.select("img.lazyload")
        for index, image in enumerate(images):
            image_url = image["data-original"]
            image_request = session.get(image_url)
            image_request.raise_for_status()
            image_bytes = image_request.content

            if "drm.nicoseiga.jp" in image_url:
                key_match = SEIGA_DRM_KEY_RE.search(image_url)
                if key_match:
                    key = key_match.group(1)
                else:
                    raise FormatNotSupportedException("Could not succesffully extract DRM key")
                image_bytes = decrypt_seiga_drm(image_bytes, key)

            data_type = determine_seiga_file_type(image_bytes)

            filename = str(index) + "." + data_type
            image_path = os.path.join(chapter_directory, filename)

            with open(image_path, "wb") as file:
                output("\rPage {0}/{1}".format(index + 1, len(images)), logging.DEBUG)
                file.write(image_bytes)

        output("\n", logging.DEBUG)
        output("Finished downloading {0} to \"{1}\".\n".format(chapter_id, chapter_directory), logging.INFO)

    if cmdl_opts.dump_metadata:
        metadata_path = os.path.join(chapter_directory, "metadata.json")
        dump_metadata(metadata_path, template_params)
    if cmdl_opts.download_thumbnail:
        output("Downloading thumbnails for Seiga comics is not currently supported.", logging.WARNING)
    if cmdl_opts.download_comments:
        output("Downloading comments for Seiga comics is not currently supported.", logging.WARNING)


def download_manga(session, manga_id):
    """Download all chapters for a requested Seiga manga."""

    output("Downloading comic {0}...\n".format(manga_id), logging.INFO)

    response = session.get(SEIGA_MANGA_URL.format(manga_id))
    response.raise_for_status()

    document = BeautifulSoup(response.text, "html.parser")
    chapters = document.select("div.episode .title a")
    for index, chapter in enumerate(chapters):
        chapter_id = chapter["href"].lstrip("/watch/").split("?")[0]
        output("{0}/{1}\n".format(index + 1, len(chapters)), logging.INFO)
        download_manga_chapter(session, chapter_id)


def download_image(session, image_id):
    """Download an individual Seiga image."""

    response = session.get(SEIGA_IMAGE_URL.format(image_id))
    response.raise_for_status()

    document = BeautifulSoup(response.text, "html.parser")
    template_params = {}
    template_params = collect_seiga_image_parameters(session, document, template_params)

    filename = create_filename(template_params)

    if not cmdl_opts.skip_media:
        output("Downloading {0} to \"{1}\"...\n".format(image_id, filename), logging.INFO)

        source_image = session.get(template_params["url"], stream=True)
        source_image.raise_for_status()

        with open(filename, "wb") as file:
            for block in source_image.iter_content(BLOCK_SIZE):
                file.write(block)

        output("Finished donwloading {0} to \"{1}\".\n".format(image_id, filename), logging.INFO)

    if cmdl_opts.dump_metadata:
        dump_metadata(filename, template_params)
    if cmdl_opts.download_thumbnail:
        output("Downloading thumbnails for Seiga images is not currently supported.", logging.WARNING)
    if cmdl_opts.download_comments:
        output("Downloading comments for Seiga images is not currently supported.", logging.WARNING)


## Video methods

def request_video(session, video_id):
    """Request the video page and initiate download of the video URL."""

    # Determine whether to request the Flash or HTML5 player
    # Only .mp4 videos are served on the HTML5 player, so we can sometimes miss the high quality .flv source
    response = session.get(THUMB_INFO_API.format(video_id))
    response.raise_for_status()

    video_info = xml.dom.minidom.parseString(response.text)

    if video_info.firstChild.getAttribute("status") != "ok":
        raise FormatNotAvailableException("Could not retrieve video info")

    concat_cookies = {}
    if cmdl_opts.download_english:
        concat_cookies = {**concat_cookies, **EN_COOKIE}

    # This is the file type for the original encode
    # When logged out, Flash videos will sometimes be served on the HTML5 player with a low quality .mp4 re-encode
    # Some Flash videos are not available outside of the Flash player
    video_type = video_info.getElementsByTagName("movie_type")[0].firstChild.nodeValue
    if video_type == "swf" or video_type == "flv":
        concat_cookies = {**concat_cookies, **FLASH_COOKIE}
    elif video_type == "mp4":
        concat_cookies = {**concat_cookies, **HTML5_COOKIE}
    else:
        raise FormatNotAvailableException("Video type not supported")

    response = session.get(VIDEO_URL.format(video_id), cookies=concat_cookies)
    response.raise_for_status()

    document = BeautifulSoup(response.text, "html.parser")

    template_params = perform_api_request(session, document)

    filename = create_filename(template_params)

    if not cmdl_opts.skip_media:
        download_video(session, filename, template_params)
    if cmdl_opts.dump_metadata:
        dump_metadata(filename, template_params)
    if cmdl_opts.download_thumbnail:
        download_thumbnail(session, filename, template_params)
    if cmdl_opts.download_comments:
        download_comments(session, filename, template_params)


def request_user(session, user_id):
    """Request videos associated with a user."""

    output("Requesting videos from user {0}...\n".format(user_id), logging.INFO)
    page_counter = 1
    video_ids = []

    # Dumb loop, process pages until we reach a page with no videos
    while True:
        user_videos_page = session.get(USER_VIDEOS_URL.format(user_id, page_counter))
        user_videos_page.raise_for_status()

        user_videos_document = BeautifulSoup(user_videos_page.text, "html.parser")
        video_links = user_videos_document.select(".VideoItem-videoDetail h5 a")

        if len(video_links) == 0:
            break

        for link in video_links:
            unstripped_id = link["href"]
            video_ids.append(unstripped_id.lstrip("watch/"))

        page_counter += 1

    total_ids = len(video_ids)
    if total_ids == 0:
        raise ParameterExtractionException("Failed to collect user videos. Please verify that the user's videos page is public")

    for index, video_id in enumerate(video_ids):
        try:
            output("{0}/{1}\n".format(index + 1, total_ids), logging.INFO)
            request_video(session, video_id)

        except (FormatNotSupportedException, FormatNotAvailableException, ParameterExtractionException) as error:
            log_exception(error)
            traceback.print_exc()
            continue


def request_mylist(session, mylist_id):
    """Request videos associated with a mylist."""

    output("Requesting mylist {0}...\n".format(mylist_id), logging.INFO)
    mylist_request = session.get(MYLIST_API.format(mylist_id))
    mylist_request.raise_for_status()
    mylist_json = json.loads(mylist_request.text)

    items = mylist_json.get("items", [])
    if mylist_json.get("status") != "ok":
        raise FormatNotAvailableException("Could not retrieve mylist info; response=" + mylist_request.text)
    else:
        for index, item in enumerate(items):
            try:
                output("{0}/{1}\n".format(index + 1, len(items)), logging.INFO)
                request_video(session, item["video_id"])

            except (FormatNotSupportedException, FormatNotAvailableException, ParameterExtractionException) as error:
                log_exception(error)
                traceback.print_exc()
                continue


def show_multithread_progress(video_len):
    """Track overall download progress across threads."""

    global progress, start_time
    finished = False
    while not finished:
        if progress >= video_len:
            finished = True
        done = int(25 * progress / video_len)
        percent = int(100 * progress / video_len)
        speed_str = calculate_speed(start_time, time.time(), progress)
        output("\r|{0}{1}| {2}/100 @ {3:9}/s".format("#" * done, " " * (25 - done), percent, speed_str), logging.DEBUG)


def update_multithread_progress(bytes_len):
    """Acquire lock on global download progress and update."""

    lock = threading.Lock()
    lock.acquire()
    try:
        global progress
        progress += bytes_len
    finally:
        lock.release()


def download_video_part(start, end, filename, session, url):
    """Download a video part using specified start and end byte boundaries."""

    resume_header = {"Range": "bytes={0}-{1}".format(start, end)}

    dl_stream = session.get(url, headers=resume_header, stream=True)
    dl_stream.raise_for_status()
    stream_iterator = dl_stream.iter_content(BLOCK_SIZE)

    part_length = end - start
    current_pos = start

    with open(filename, "r+b") as file:
        file.seek(current_pos)
        for block in stream_iterator:
            current_pos += len(block)
            file.write(block)
            update_multithread_progress(len(block))
    update_multithread_progress(-1) # NUL byte at end of each part


def download_video(session, filename, template_params):
    """Download video from response URL and display progress."""

    output("Downloading {0} to \"{1}\"...\n".format(template_params["id"], filename), logging.INFO)

    dl_stream = session.head(template_params["url"])
    dl_stream.raise_for_status()
    video_len = int(dl_stream.headers["content-length"])

    if cmdl_opts.threads:
        output("Multithreading is experimental and will overwrite any existing files.\n", logging.WARNING)

        threads = int(cmdl_opts.threads)
        if threads <= 0:
            raise ArgumentException("Thread number must be a positive integer")

        # Track total bytes downloaded across threads
        global progress
        progress = 0

        # Pad out file to full length
        file = open(filename, "wb")
        file.truncate(video_len)
        file.close()

        # Calculate ranges for threads and dispatch
        part = math.ceil(video_len / threads)

        global start_time
        start_time = time.time()

        for i in range(threads):
            start = part * i
            end = video_len if i == threads - 1 else start + part

            thread = threading.Thread(target=download_video_part, kwargs={"start": start, "end": end, "filename": filename, "session": session, "url": template_params["url"]})
            thread.setDaemon(True)
            thread.start()

        progress_thread = threading.Thread(target=show_multithread_progress, kwargs={"video_len": video_len})
        progress_thread.start()

        # Join threads
        main_thread = threading.current_thread()
        for thread in threading.enumerate():
            if thread is main_thread:
                continue
            thread.join()
        output("\n", logging.DEBUG)

        output("Finished downloading {0} to \"{1}\".\n".format(template_params["id"], filename), logging.INFO)
        return

    if os.path.isfile(filename):
        with open(filename, "rb") as file:
            current_byte_pos = os.path.getsize(filename)
            if current_byte_pos < video_len:
                file_condition = "ab"
                resume_header = {"Range": "bytes={0}-".format(current_byte_pos - BLOCK_SIZE)}
                dl = current_byte_pos - BLOCK_SIZE
                output("Checking file integrity before resuming.\n")

            elif current_byte_pos > video_len:
                raise FormatNotAvailableException("Current byte position exceeds the length of the video to be downloaded. Check the interity of the existing file and use --force-high-quality to resume this download when the high quality source is available.\n")

            # current_byte_pos == video_len
            else:
                output("File exists and matches current download length.\n", logging.INFO)
                return

    else:
        file_condition = "wb"
        resume_header = {"Range": "bytes=0-"}
        dl = 0

    dl_stream = session.get(template_params["url"], headers=resume_header, stream=True)
    dl_stream.raise_for_status()
    stream_iterator = dl_stream.iter_content(BLOCK_SIZE)

    if os.path.isfile(filename):
        new_data = next(stream_iterator)
        new_data_len = len(new_data)

        existing_byte_pos = os.path.getsize(filename)
        if current_byte_pos - new_data_len <= 0:
            output("Byte comparison block exceeds the length of the existing file. Deleting existing file and redownloading...\n")
            os.remove(filename)
            download_video(session, filename, template_params)
            return

        with open(filename, "rb") as file:
            file.seek(current_byte_pos - BLOCK_SIZE)
            existing_data = file.read()[:new_data_len]
            if new_data == existing_data:
                dl += new_data_len
                output("Resuming at byte position {0}.\n".format(dl))
            else:
                output("Byte comparison block does not match. Deleting existing file and redownloading...\n")
                os.remove(filename)
                download_video(session, filename, template_params)
                return

    with open(filename, file_condition) as file:
        file.seek(dl)
        start_time = time.time()
        for block in stream_iterator:
            dl += len(block)
            file.write(block)
            done = int(25 * dl / video_len)
            percent = int(100 * dl / video_len)
            speed_str = calculate_speed(start_time, time.time(), dl)
            output("\r|{0}{1}| {2}/100 @ {3:9}/s".format("#" * done, " " * (25 - done), percent, speed_str), logging.DEBUG)
        output("\n", logging.DEBUG)

    output("Finished downloading {0} to \"{1}\".\n".format(template_params["id"], filename), logging.INFO)


def perform_heartbeat(session, heartbeat_url, response):
    """Perform a response heartbeat to keep the video download connection alive."""

    response = session.post(heartbeat_url, data=response.toxml())
    response.raise_for_status()
    response = xml.dom.minidom.parseString(response.text).getElementsByTagName("session")[0]
    heartbeat_timer = threading.Timer(DMC_HEARTBEAT_INTERVAL_S, perform_heartbeat, (session, heartbeat_url, response))
    heartbeat_timer.daemon = True
    heartbeat_timer.start()


def determine_quality(template_params, params):
    """Determine the quality parameter for all videos."""

    if params.get("video"):
        if params["video"].get("dmcInfo"):
            if params["video"]["dmcInfo"]["quality"]["videos"][0]["id"] == template_params["video_quality"] and params["video"]["dmcInfo"]["quality"]["audios"][0]["id"] == template_params["audio_quality"]:
                template_params["quality"] = "auto"
            else:
                template_params["quality"] = "low"

        elif params["video"].get("smileInfo"):
            template_params["quality"] = params["video"]["smileInfo"]["currentQualityId"]

    if params.get("videoDetail"):
        template_params["quality"] = "auto"


def select_dmc_quality(template_params, template_key, sources: list, quality=None):
    """Select the specified quality from a sources list on DMC videos."""

    # TODO: Make sure source is available
    # Haven't seen a source marked as unavailable in the wild rather than be unlisted, but we might as well be sure

    if quality and cmdl_opts.force_high_quality:
        output("Video or audio quality specified with --force-high-quality. Ignoring quality...\n", logging.WARNING)

    if not quality or cmdl_opts.force_high_quality or quality.lower() == "highest":
        template_params[template_key] = sources[:1][0]
        return sources[:1]

    if quality.lower() == "lowest":
        template_params[template_key] = sources[-1:][0]
        return sources[-1:]

    filtered = list(filter(lambda q: q.lower() == quality.lower(), sources))
    if not filtered:
        raise FormatNotAvailableException(f"Quality '{quality}' is not available. Available qualities: {sources}")

    template_params[template_key] = filtered[:1][0]
    return filtered[:1]


def perform_api_request(session, document):
    """Collect parameters from video document and build API request for video URL."""

    template_params = {}

    # .mp4 videos (HTML5)
    if document.find(id="js-initial-watch-data"):
        params = json.loads(document.find(id="js-initial-watch-data")["data-api-data"])

        if params["video"]["isDeleted"]:
            raise FormatNotAvailableException("Video was deleted")

        template_params = collect_parameters(session, template_params, params, is_html5=True)

        # Perform request to Dwango Media Cluster (DMC)
        if params["video"].get("dmcInfo"):
            api_url = params["video"]["dmcInfo"]["session_api"]["urls"][0]["url"] + "?suppress_response_codes=true&_format=xml"
            recipe_id = params["video"]["dmcInfo"]["session_api"]["recipe_id"]
            content_id = params["video"]["dmcInfo"]["session_api"]["content_id"]
            protocol = params["video"]["dmcInfo"]["session_api"]["protocols"][0]
            file_extension = template_params["ext"]
            priority = params["video"]["dmcInfo"]["session_api"]["priority"]

            video_sources = select_dmc_quality(template_params, "video_quality", params["video"]["dmcInfo"]["session_api"]["videos"], cmdl_opts.video_quality)
            audio_sources = select_dmc_quality(template_params, "audio_quality", params["video"]["dmcInfo"]["session_api"]["audios"], cmdl_opts.audio_quality)
            determine_quality(template_params, params)
            if template_params["quality"] != "auto" and cmdl_opts.force_high_quality:
                raise FormatNotAvailableException("High quality source is not available")

            heartbeat_lifetime = params["video"]["dmcInfo"]["session_api"]["heartbeat_lifetime"]
            token = params["video"]["dmcInfo"]["session_api"]["token"]
            signature = params["video"]["dmcInfo"]["session_api"]["signature"]
            auth_type = params["video"]["dmcInfo"]["session_api"]["auth_types"]["http"]
            service_user_id = params["video"]["dmcInfo"]["session_api"]["service_user_id"]
            player_id = params["video"]["dmcInfo"]["session_api"]["player_id"]

            # Build initial heartbeat request
            post = """
                    <session>
                      <recipe_id>{0}</recipe_id>
                      <content_id>{1}</content_id>
                      <content_type>movie</content_type>
                      <protocol>
                        <name>{2}</name>
                        <parameters>
                          <http_parameters>
                            <method>GET</method>
                            <parameters>
                              <http_output_download_parameters>
                                <file_extension>{3}</file_extension>
                              </http_output_download_parameters>
                            </parameters>
                          </http_parameters>
                        </parameters>
                      </protocol>
                      <priority>{4}</priority>
                      <content_src_id_sets>
                        <content_src_id_set>
                          <content_src_ids>
                            <src_id_to_mux>
                              <video_src_ids>
                              </video_src_ids>
                              <audio_src_ids>
                              </audio_src_ids>
                            </src_id_to_mux>
                          </content_src_ids>
                        </content_src_id_set>
                      </content_src_id_sets>
                      <keep_method>
                        <heartbeat>
                          <lifetime>{5}</lifetime>
                        </heartbeat>
                      </keep_method>
                      <timing_constraint>unlimited</timing_constraint>
                      <session_operation_auth>
                        <session_operation_auth_by_signature>
                          <token>{6}</token>
                          <signature>{7}</signature>
                        </session_operation_auth_by_signature>
                      </session_operation_auth>
                      <content_auth>
                        <auth_type>{8}</auth_type>
                        <service_id>nicovideo</service_id>
                        <service_user_id>{9}</service_user_id>
                        <max_content_count>10</max_content_count>
                        <content_key_timeout>600000</content_key_timeout>
                      </content_auth>
                      <client_info>
                        <player_id>{10}</player_id>
                      </client_info>
                    </session>
                """.format(recipe_id,
                           content_id,
                           protocol,
                           file_extension,
                           priority,
                           heartbeat_lifetime,
                           token,
                           signature,
                           auth_type,
                           service_user_id,
                           player_id).strip()

            root = xml.dom.minidom.parseString(post)
            sources = root.getElementsByTagName("video_src_ids")[0]
            for video_source in video_sources:
                element = root.createElement("string")
                quality = root.createTextNode(video_source)
                element.appendChild(quality)
                sources.appendChild(element)

            sources = root.getElementsByTagName("audio_src_ids")[0]
            for audio_source in audio_sources:
                element = root.createElement("string")
                quality = root.createTextNode(audio_source)
                element.appendChild(quality)
                sources.appendChild(element)

            output("Performing initial API request...\n", logging.INFO)
            headers = {"Content-Type": "application/xml"}
            response = session.post(api_url, headers=headers, data=root.toxml())
            response.raise_for_status()
            response = xml.dom.minidom.parseString(response.text)
            template_params["url"] = response.getElementsByTagName("content_uri")[0].firstChild.nodeValue
            output("Performed initial API request.\n", logging.INFO)

            # Collect response for heartbeat
            session_id = response.getElementsByTagName("id")[0].firstChild.nodeValue
            response = response.getElementsByTagName("session")[0]
            heartbeat_url = params["video"]["dmcInfo"]["session_api"]["urls"][0]["url"] + "/" + session_id + "?_format=xml&_method=PUT"
            perform_heartbeat(session, heartbeat_url, response)

        # Legacy URL for videos uploaded pre-HTML5 player (~2016-10-27)
        elif params["video"].get("smileInfo"):
            output("Using legacy URL...\n", logging.INFO)

            if cmdl_opts.video_quality or cmdl_opts.audio_quality:
                output("Video and audio qualities can't be specified on legacy videos. Ignoring...\n", logging.WARNING)
            determine_quality(template_params, params)
            if template_params["quality"] != "auto" and cmdl_opts.force_high_quality:
                raise FormatNotAvailableException("High quality source is not available")

            template_params["url"] = params["video"]["smileInfo"]["url"]

        else:
            raise ParameterExtractionException("Failed to find video URL. Nico may have updated their player")

    # Flash videos (.flv, .swf)
    # NicoMovieMaker videos (.swf) may need conversion to play properly in an external player
    elif document.find(id="watchAPIDataContainer"):
        params = json.loads(document.find(id="watchAPIDataContainer").text)

        if params["videoDetail"]["isDeleted"]:
            raise FormatNotAvailableException("Video was deleted")

        template_params = collect_parameters(session, template_params, params, is_html5=False)

        if cmdl_opts.video_quality or cmdl_opts.audio_quality:
            output("Video and audio qualities can't be specified on Flash videos. Ignoring...\n", logging.WARNING)
        determine_quality(template_params, params)
        if template_params["quality"] != "auto" and cmdl_opts.force_high_quality:
            raise FormatNotAvailableException("High quality source is not available")

        video_url_param = urllib.parse.parse_qs(urllib.parse.unquote(urllib.parse.unquote(params["flashvars"]["flvInfo"])))
        if ("url" in video_url_param):
            template_params["url"] = video_url_param["url"][0]

        else:
            raise ParameterExtractionException("Failed to find video URL. Nico may have updated their player")

    else:
        raise ParameterExtractionException("Failed to collect video paramters")

    return template_params


# Metadata extraction

def collect_parameters(session, template_params, params, is_html5):
    """Collect video parameters to make them available for an output filename template."""

    if params.get("video"):
        template_params["id"] = params["video"]["id"]
        template_params["title"] = params["video"]["title"]
        template_params["uploader"] = params["owner"]["nickname"].rstrip(" さん") if params.get("owner") else None
        template_params["uploader_id"] = int(params["owner"]["id"]) if params.get("owner") else None
        template_params["description"] = params["video"]["description"]
        template_params["thumbnail_url"] = params["video"]["thumbnailURL"]
        template_params["thread_id"] = int(params["thread"]["ids"]["default"])
        template_params["published"] = params["video"]["postedDateTime"]
        template_params["duration"] = params["video"]["duration"]
        template_params["view_count"] = params["video"]["viewCount"]
        template_params["mylist_count"] = params["video"]["mylistCount"]
        template_params["comment_count"] = params["thread"]["commentCount"]

    elif params.get("videoDetail"):
        template_params["id"] = params["videoDetail"]["id"]
        template_params["title"] = params["videoDetail"]["title"]
        template_params["uploader"] = params["uploaderInfo"]["nickname"].rstrip(" さん") if params.get("uploaderInfo") else None
        template_params["uploader_id"] = int(params["uploaderInfo"]["id"]) if params.get("uploaderInfo") else None
        template_params["description"] = params["videoDetail"]["description"]
        template_params["thumbnail_url"] = params["videoDetail"]["thumbnail"]
        template_params["thread_id"] = int(params["videoDetail"]["thread_id"])
        template_params["published"] = params["videoDetail"]["postedAt"]
        template_params["duration"] = params["videoDetail"]["length"]
        template_params["view_count"] = params["videoDetail"]["viewCount"]
        template_params["mylist_count"] = params["videoDetail"]["mylistCount"]
        template_params["comment_count"] = params["videoDetail"]["commentCount"]

    response = session.get(THUMB_INFO_API.format(template_params["id"]))
    response.raise_for_status()
    video_info = xml.dom.minidom.parseString(response.text)

    # DMC videos do not expose the file type in the video page parameters when not logged in
    # If this is a Flash video being served on the HTML5 player, it's guaranteed to be a low quality .mp4 re-encode
    template_params["ext"] = video_info.getElementsByTagName("movie_type")[0].firstChild.nodeValue
    if is_html5 and (template_params["ext"] == "swf" or template_params["ext"] == "flv"):
        template_params["ext"] = "mp4"

    template_params["size_high"] = int(video_info.getElementsByTagName("size_high")[0].firstChild.nodeValue)
    template_params["size_low"] = int(video_info.getElementsByTagName("size_low")[0].firstChild.nodeValue)

    # Check if we couldn't capture uploader info before
    if not template_params["uploader_id"]:
        channel_id = video_info.getElementsByTagName("ch_id")
        user_id = video_info.getElementsByTagName("user_id")
        template_params["uploader_id"] = channel_id[0].firstChild.nodeValue if channel_id else user_id[0].firstChild.nodeValue if user_id else None

    if not template_params["uploader"]:
        channel_name = video_info.getElementsByTagName("ch_name")
        user_nickname = video_info.getElementsByTagName("user_nickname")
        template_params["uploader"] = channel_name[0].firstChild.nodeValue if channel_name else user_nickname[0].firstChild.nodeValue if user_nickname else None

    return template_params


def dump_metadata(filename, template_params):
    """Dump the collected video metadata to a file."""

    output("Downloading metadata for {0}...\n".format(template_params["id"]), logging.INFO)

    filename = replace_extension(filename, "json")

    with open(filename, "w") as file:
        json.dump(template_params, file, sort_keys=True)

    output("Finished downloading metadata for {0}.\n".format(template_params["id"]), logging.INFO)


def download_thumbnail(session, filename, template_params):
    """Download the video thumbnail."""

    output("Downloading thumbnail for {0}...\n".format(template_params["id"]), logging.INFO)

    filename = replace_extension(filename, "jpg")

    # Try to retrieve the large thumbnail
    get_thumb = session.get(template_params["thumbnail_url"] + ".L")
    if get_thumb.status_code == 404:
        get_thumb = session.get(template_params["thumbnail_url"])
        get_thumb.raise_for_status()

    with open(filename, "wb") as file:
        for block in get_thumb.iter_content(BLOCK_SIZE):
            file.write(block)

    output("Finished downloading thumbnail for {0}.\n".format(template_params["id"]), logging.INFO)


def download_comments(session, filename, template_params):
    """Download the video comments."""

    output("Downloading comments for {0}...\n".format(template_params["id"]), logging.INFO)

    filename = replace_extension(filename, "xml")

    if cmdl_opts.download_english:
        post_packet = COMMENTS_POST_EN
    else:
        post_packet = COMMENTS_POST_JP
    get_comments = session.post(COMMENTS_API, post_packet.format(template_params["thread_id"]))
    get_comments.raise_for_status()
    with open(filename, "wb") as file:
        file.write(get_comments.content)

    output("Finished downloading comments for {0}.\n".format(template_params["id"]), logging.INFO)


## Main entry

def login(username, password):
    """Login to Nico and create a session."""

    session = requests.session()

    retry = Retry(
        total=RETRY_ATTEMPTS,
        read=RETRY_ATTEMPTS,
        connect=RETRY_ATTEMPTS,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=(500, 502, 503, 504),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    session.headers.update({"User-Agent": "nndownload/{0}".format(__version__)})

    if cmdl_opts.proxy:
        proxies = {
            "http": cmdl_opts.proxy,
            "https": cmdl_opts.proxy
        }
        session.proxies.update(proxies)

    if not cmdl_opts.no_login:
        output("Logging in...\n", logging.INFO)

        login_post = {
            "mail_tel": username,
            "password": password
        }

        response = session.post(LOGIN_URL, data=login_post)
        response.raise_for_status()
        if not session.cookies.get_dict().get("user_session", None):
            output("Failed to login.\n", logging.INFO)
            raise AuthenticationException("Failed to login. Please verify your username and password")

        output("Logged in.\n", logging.INFO)

    return session


def valid_url(url):
    """Check if the URL is valid and can be processed."""

    url_mo = VIDEO_URL_RE.match(url)
    return url_mo if not None else False


def process_url_mo(session, url_mo):
    """Determine which function should process this URL object."""

    url_id = url_mo.group(4)
    if url_mo.group(3) == "mylist":
        request_mylist(session, url_id)
    elif url_mo.group(2):
        request_nama(session, url_id)
    elif url_mo.group(3) == "user":
        request_user(session, url_id)
    elif url_mo.group(1) == "seiga":
        if url_mo.group(3) == "watch":
            download_manga_chapter(session, url_id)
        elif url_mo.group(3) == "comic":
            download_manga(session, url_id)
        else:
            download_image(session, url_id)
    else:
        request_video(session, url_id)


def main():
    try:
        configure_logger()
        # Test if input is a valid URL or file
        url_mo = valid_url(cmdl_opts.input)
        if not url_mo:
            open(cmdl_opts.input)

        account_username = cmdl_opts.username
        account_password = cmdl_opts.password

        if cmdl_opts.netrc:
            if cmdl_opts.username or cmdl_opts.password:
                output("Ignorning input credentials in favor of .netrc.\n", logging.WARNING)

            account_credentials = netrc.netrc().authenticators(HOST)
            if account_credentials:
                account_username = account_credentials[0]
                account_password = account_credentials[2]
            else:
                raise netrc.NetrcParseError("No authenticator available for {0}".format(HOST))
        elif not cmdl_opts.no_login:
            if not account_username:
                account_username = input("Username: ")
            if not account_password:
                account_password = getpass.getpass("Password: ")
        else:
            output("Proceeding with no login. Some videos may not be available for download or may only be available in a lower quality. For access to all videos, please provide a login with --username/--password or --netrc.\n", logging.WARNING)

        session = login(account_username, account_password)
        if url_mo:
            process_url_mo(session, url_mo)
        else:
            read_file(session, cmdl_opts.input)

    except Exception as error:
        log_exception(error)
        raise


if __name__ == "__main__":
    try:
        cmdl_opts = cmdl_parser.parse_args()
        main()
    except KeyboardInterrupt:
        sys.exit(1)
