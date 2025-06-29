# poc_digital_signage.py

import os
import requests
import time
from datetime import datetime, UTC
import json
import re
from html.parser import HTMLParser
import urllib.parse
import subprocess
import signal
import sys

# --- Configuration ---
# IMPORTANT: This path must be EXACTLY correct for your user and desktop location.
# Adjusted for Raspberry Pi's Linux filesystem.
DOWNLOAD_DIR = '/home/josevelazquez/signage'

# Vimeo API Credentials - Your Personal Access Token (with 'video_files' scope enabled)
# REMINDER: This token must be VALID and have the correct scopes!
VIMEO_ACCESS_TOKEN = "{TOKEN}" # <--- UPDATE THIS IF YOU GET 401 ERROR

# Specific Vimeo Project/Album ID for "Collection"
VIMEO_PROJECT_ID = "{PROJECT_ID}"

# File to store the index of the last played video to maintain order
LAST_PLAYED_INDEX_FILE = os.path.join(DOWNLOAD_DIR, "last_played_index.json")

LOG_FILE = os.path.join(DOWNLOAD_DIR, "poc_log.log")

# --- Interval for continuous loop (2 minutes cycle duration) ---
# This is the target fixed duration from the START of one cycle to the START of the next.
TARGET_CYCLE_DURATION_SECONDS = 120 # 2 minutes * 60 seconds/minute

# --- Playback Configuration ---
# Set to a number (e.g., 30) to play each video for a fixed duration.
# If None, it attempts to play the entire video duration (within TARGET_CYCLE_DURATION_SECONDS).
PLAY_DURATION_SECONDS = None

# This is the absolute minimum time FFplay will attempt to play a video for,
# in case its reported duration is 0 or very short, ensuring visibility.
MIN_PLAY_DURATION_SECONDS = 5

# --- Vimeo API Base URL and Headers for Direct Calls ---
VIMEO_API_BASE_URL = "https://api.vimeo.com"
VIMEO_HEADERS = {
    "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}",
    "Accept": "application/vnd.vimeo.*+json;version=3.4"
}

# --- Global variable to track the currently playing ffplay process ---
current_ffplay_process = None


# --- HTMLParser to extract iframe src ---
class IframeSrcExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.src = None
        self.in_iframe = False

    def handle_starttag(self, tag, attrs):
        if tag == 'iframe':
            self.in_iframe = True
            for attr, value in attrs:
                if attr == 'src':
                    self.src = value
                    break

    def handle_endtag(self, tag):
        if tag == 'iframe':
            self.in_iframe = False

    def get_src(self, html_string):
        self.src = None
        self.feed(html_string)
        self.close()
        return self.src


def log_message(message, level="INFO"):
    """Logs messages to a file and prints to console with a level."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {level.upper()}: {message}"

    print(log_entry) # Always print to console/stdout

    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"[{timestamp}] ERROR: Error writing to log file: {e}")

def cleanup_download_directory():
    """Deletes all .mp4 files in the download directory."""
    log_message(f"STARTING CLEANUP for {DOWNLOAD_DIR}", "DEBUG")
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        log_message(f"Created missing download directory: {DOWNLOAD_DIR}", "INFO")
        return

    files_deleted = []
    for filename in os.listdir(DOWNLOAD_DIR):
        if filename.endswith(".mp4"): # Still check for MP4, as original files are video
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            try:
                os.remove(filepath)
                files_deleted.append(filename)
                log_message(f"Deleted file: {filename}", "INFO")
            except OSError as e:
                log_message(f"Failed to delete {filename}: {e}", "ERROR")

    if files_deleted:
        log_message(f"Cleanup complete. Deleted {len(files_deleted)} MP4 files.", "INFO")
    else:
        log_message("Cleanup complete. No MP4 files found to delete.", "INFO")


def get_all_vimeo_videos_in_order():
    """
    Fetches all videos from the specified Vimeo Project/Album,
    sorts them numerically by 'BLACKLIVES_BL_XXX' name,
    and tries to find a downloadable MP4 link from 'files', 'download', or 'embed.html'.
    """
    log_message("Fetching all videos from Vimeo project...", "INFO")
    all_videos_data = []

    current_page_url = f"{VIMEO_API_BASE_URL}/me/projects/{VIMEO_PROJECT_ID}/videos"

    try:
        while current_page_url:
            log_message(f"Requesting Vimeo API page: {current_page_url}", "DEBUG")
            response = requests.get(
                current_page_url,
                headers=VIMEO_HEADERS,
                params={
                    'per_page': 100,
                    'fields': 'uri,name,files,download,embed.html,duration'
                },
                timeout=30,
                verify=False # <--- TEMPORARY WORKAROUND FOR SSLError - REMOVE IN PRODUCTION
            )

            response.raise_for_status()

            data = response.json()
            all_videos_data.extend(data.get('data', []))
            current_page_url = data.get('paging', {}).get('next')

        if not all_videos_data:
            log_message("No videos found in the specified Vimeo Project. Check project ID and permissions.", "WARNING")
            return []

        downloadable_videos = []
        iframe_parser = IframeSrcExtractor()

        for video in all_videos_data:
            best_file_link_url = None
            link_source = "None"
            best_quality_info = None

            def find_best_mp4_link_in_array(file_list):
                best = None
                for f in file_list:
                    # We are looking for MP4 files as these typically contain both audio and video
                    # We will then instruct ffplay to only play the audio.
                    if f.get('type') == 'video/mp4' and f.get('link') and f.get('width') is not None and f.get('height') is not None:
                        if best is None or f['width'] * f['height'] > best['width'] * f['height']:
                            best = f
                return best

            log_message(f"--- Processing Video '{video['name']}' ({video['uri'].split('/')[-1]}) ---", "DEBUG")
            log_message(f"  Raw Duration from API: {video.get('duration', 'N/A')}", "DEBUG")

            # 1. PRIORITY: Try to find the best MP4 in 'files' field
            if 'files' in video and isinstance(video['files'], list):
                found_file = find_best_mp4_link_in_array(video['files'])
                if found_file:
                    best_quality_info = found_file
                    best_file_link_url = found_file['link']
                    link_source = "files"
                    log_message(f"  Found BEST link in 'files' field: {found_file.get('quality', 'N/A')} - {found_file['link']}", "DEBUG")

            # 2. FALLBACK: If no suitable file in 'files', try 'download' field
            if best_file_link_url is None and 'download' in video and isinstance(video['download'], list):
                found_download = find_best_mp4_link_in_array(video['download'])
                if found_download:
                    best_quality_info = found_download
                    best_file_link_url = found_download['link']
                    link_source = "download"
                    log_message(f"  Found BEST link in 'download' field: {found_download.get('quality', 'N/A')} - {found_download['link']}", "DEBUG")

            # 3. LAST RESORT: Extract from 'embed.html' if no direct file links found
            if best_file_link_url is None and 'embed' in video and 'html' in video['embed']:
                embed_html = video['embed']['html']
                extracted_src = iframe_parser.get_src(embed_html)
                if extracted_src:
                    parsed_url = urllib.parse.urlparse(extracted_src)
                    query_params = urllib.parse.parse_qs(parsed_url.query)
                    query_params['autoplay'] = ['1']
                    reconstructed_url = urllib.parse.urlunparse(
                        parsed_url._replace(query=urllib.parse.urlencode(query_params, doseq=True))
                    )
                    best_file_link_url = reconstructed_url
                    link_source = "embed_html"
                    log_message(f"  Extracted embed link (likely 403 on download): {best_file_link_url}", "DEBUG")

            if best_file_link_url:
                log_message(f"Selected '{link_source}' link for: '{video['name']}'", "INFO")
                downloadable_videos.append({
                    'id': video['uri'].split('/')[-1],
                    'title': video['name'],
                    'download_link': best_file_link_url,
                    'quality': best_quality_info.get('quality', 'N/A') if best_quality_info else 'N/A',
                    'size': best_quality_info.get('size', 'N/A') if best_quality_info else 'N/A',
                    'created_time': video.get('created_time', '1970-01-01T00:00:00+00:00'),
                    'duration': video.get('duration', 0)
                })
            else:
                log_message(f"NO USABLE MP4 LINK FOUND FOR: '{video.get('name', 'Unknown')}' (ID: {video['uri'].split('/')[-1]}). Please check Vimeo account/video settings.", "CRITICAL")
                log_message(f"  Raw 'files' data: {video.get('files', 'Not present')}", "DEBUG")
                log_message(f"  Raw 'download' data: {video.get('download', 'Not present')}", "DEBUG")


        def get_bl_number(video_name):
            match = re.search(r'BL_(\d+)', video_name)
            return int(match.group(1)) if match else float('inf')

        downloadable_videos.sort(key=lambda x: get_bl_number(x['title']))

        log_message(f"Successfully fetched and sorted {len(downloadable_videos)} potentially downloadable videos.", "INFO")
        return downloadable_videos

    except requests.exceptions.RequestException as e:
        log_message(f"HTTP/Network error during Vimeo API call: {e}", "ERROR")
        return []
    except json.JSONDecodeError as e:
        log_message(f"Error decoding JSON response from Vimeo API: {e}. Response might be invalid.", "ERROR")
        return []
    except Exception as e:
        log_message(f"An unexpected error occurred during Vimeo API video fetching/sorting: {e}", "CRITICAL")
        return []

def get_next_video_in_sequence(all_videos):
    """
    Determines the next video to download based on the last played index.
    Updates the index file.
    """
    log_message("Determining next video in sequence...", "INFO")
    if not all_videos:
        log_message("No videos provided to determine next in sequence.", "WARNING")
        return None, 0

    current_index = 0
    if os.path.exists(LAST_PLAYED_INDEX_FILE):
        try:
            with open(LAST_PLAYED_INDEX_FILE, 'r') as f:
                data = json.load(f)
                current_index = data.get('last_index', 0)
                log_message(f"Loaded last_index: {current_index} from {LAST_PLAYED_INDEX_FILE}", "DEBUG")
        except json.JSONDecodeError:
            log_message("Error reading last_played_index.json. File corrupted. Resetting index to 0.", "ERROR")
            current_index = 0
        except Exception as e:
            log_message(f"Unexpected error loading last_played_index.json: {e}. Resetting index to 0.", "ERROR")
            current_index = 0

    if not (0 <= current_index < len(all_videos)):
        log_message(f"Stored index {current_index} is out of bounds for {len(all_videos)} videos. Resetting to 0.", "WARNING")
        current_index = 0

    video_for_this_run = all_videos[current_index]
    next_index_for_save = (current_index + 1) % len(all_videos)

    try:
        with open(LAST_PLAYED_INDEX_FILE, 'w') as f:
            json.dump({'last_index': next_index_for_save}, f)
        log_message(f"Saved next index for the following run: {next_index_for_save}.", "INFO")
    except Exception as e:
        log_message(f"Error saving next index to {LAST_PLAYED_INDEX_FILE}: {e}", "ERROR")

    log_message(f"Determined video for current run: '{video_for_this_run['title']}' (Index: {current_index})", "INFO")
    return video_for_this_run, current_index


def download_video(video_info):
    log_message(f"Attempting to download '{video_info['title']}' from Vimeo URL: {video_info['download_link']}", "INFO")
    try:
        custom_headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://player.vimeo.com/'
        }
        response = requests.get(
            video_info['download_link'],
            stream=True,
            timeout=300,
            verify=False,
            headers=custom_headers,
            allow_redirects=True
        )
        response.raise_for_status()

        # Sanitize filename for Linux compatibility (replace problematic characters)
        safe_title = re.sub(r'[^\w\-_\. ]', '', video_info['title']).strip()
        filepath = os.path.join(DOWNLOAD_DIR, f"{video_info['id']}_{safe_title}.mp4")

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        log_message(f"SUCCESS: Downloaded '{video_info['title']}' to {filepath}", "INFO")
        return filepath
    except requests.exceptions.RequestException as e:
        log_message(f"ERROR: Download failed for '{video_info['title']}': {e}", "ERROR")
        log_message(f"  URL: {video_info['download_link']}", "ERROR")
        log_message(f"  Reason: {e.response.status_code if e.response else 'No response status'} {e.response.reason if e.response else 'No response reason'}", "ERROR")
        if os.path.exists(filepath):
            os.remove(filepath)
        return None
    except Exception as e:
        log_message(f"CRITICAL: An unexpected error occurred during download: {e}", "CRITICAL")
        return None

def play_video(filepath, video_duration_seconds=None):
    """
    Plays a video using ffplay in a non-blocking way, with optional duration limit.
    Adapted for audio-only playback on Raspberry Pi.
    Args:
        filepath (str): Path to the video file.
        video_duration_seconds (int, optional): The actual duration of the video.
                                                 Used to determine effective play duration.
    Returns:
        bool: True if playback was initiated, False otherwise.
    """
    global current_ffplay_process # Declare intent to use global variable

    if not os.path.exists(filepath):
        log_message(f"Error: Video file not found for playback: {filepath}", "ERROR")
        return False

    ffplay_command = [
        "ffplay",
        "-vn",              # <--- IMPORTANT: No video output (audio only)
        "-autoexit",        # Exit when playback finishes (if not killed manually)
        "-hide_banner",     # Hide FFmpeg build info
        "-nostats",         # Don't show playback statistics
        "-loglevel", "info", # <--- Reverted to info (less verbose)
        filepath
    ]

    # --- Determine effective play duration ---
    effective_play_duration = MIN_PLAY_DURATION_SECONDS # Start with our fixed default

    # If the API provided a valid duration and it's positive, use it, but cap it at our default if needed.
    if video_duration_seconds is not None and video_duration_seconds > 0:
        if PLAY_DURATION_SECONDS is not None: # If a fixed user-defined play duration is set
            effective_play_duration = min(PLAY_DURATION_SECONDS, video_duration_seconds)
        else: # If no fixed user-defined duration, play full video (API duration)
            effective_play_duration = video_duration_seconds

    # Ensure effective_play_duration is at least our minimum for visibility, or positive if API duration was 0
    if effective_play_duration <= 0:
        effective_play_duration = MIN_PLAY_DURATION_SECONDS # Fallback to minimum if still 0 or less


    if effective_play_duration is not None and effective_play_duration > 0:
        # Insert -t flag and duration before filepath
        # Using list.insert() is specific about position.
        # filepath is at index -1, so to insert before it, we insert at -1
        ffplay_command.insert(len(ffplay_command) - 1, "-t")
        ffplay_command.insert(len(ffplay_command) - 1, str(effective_play_duration))
        log_message(f"Playing for approx. {effective_play_duration} seconds (API duration: {video_duration_seconds}s).", "INFO")
    else: # Fallback if duration is somehow still problematic (shouldn't happen with above logic)
        log_message(f"Playing full video (API duration: {video_duration_seconds if video_duration_seconds is not None else 'N/A'}s).", "INFO")


    log_message(f"Starting playback with ffplay: {' '.join(ffplay_command)}", "INFO")
    try:
        # Start ffplay process in the background (non-blocking)
        # Store the process object globally so we can kill it later.
        # Re-added stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL to show ffplay output in logs
        current_ffplay_process = subprocess.Popen(ffplay_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log_message(f"FFplay process started with PID: {current_ffplay_process.pid}", "INFO")

        # Script does NOT wait here (no .wait()), it continues immediately.
        return True
    except FileNotFoundError:
        log_message("Error: 'ffplay' command not found. Please ensure ffmpeg is installed.", "CRITICAL")
        return False
    except Exception as e:
        log_message(f"An unexpected error occurred during ffplay playback initiation for {filepath}: {e}", "CRITICAL")
        return False

def run_one_cycle():
    """Encapsulates the full logic for one download/cleanup/play cycle."""
    global current_ffplay_process # Declare intent to use global variable
    global cycle_start_time # Access the global variable defined in __main__

    cycle_start_time = time.time() # This will now set the global variable

    log_message("\n" + "="*80, "INFO")
    log_message("--- POC Digital Signage Script STARTING NEW CYCLE ---", "INFO")
    log_message(f"Current System Time (UTC): {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}", "INFO")
    log_message("="*80 + "\n", "INFO")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # --- Take over: Kill any previous ffplay process before starting a new cycle ---
    if current_ffplay_process and current_ffplay_process.poll() is None: # poll() is None if process is still running
        log_message(f"Forcefully terminating previous ffplay process (PID: {current_ffplay_process.pid})...", "INFO")
        try:
            current_ffplay_process.terminate() # or .kill() for stronger termination
            current_ffplay_process.wait(timeout=5) # Give it a moment to terminate
            log_message("Previous ffplay process terminated.", "INFO")
        except subprocess.TimeoutExpired:
            log_message("Previous ffplay process did not terminate gracefully, killing it.", "WARNING")
            current_ffplay_process.kill()
            current_ffplay_process.wait() # Ensure it's dead
        except Exception as e:
            log_message(f"Error terminating previous ffplay process: {e}", "ERROR")
        current_ffplay_process = None # Clear the global reference

    ordered_vimeo_videos = get_all_vimeo_videos_in_order()

    if not ordered_vimeo_videos:
        log_message("No videos available to process after fetching from Vimeo. Check logs for API errors or content issues.", "CRITICAL")
        log_message("="*80, "INFO")
        log_message(f"POC Cycle ABORTED. Total duration: {time.time() - cycle_start_time:.2f} seconds.", "INFO")
        log_message("="*80 + "\n", "INFO")
        return # Ensure function exits here if no videos

    video_to_process_this_run, current_video_index = get_next_video_in_sequence(ordered_vimeo_videos)

    if not video_to_process_this_run:
        log_message("Could not determine next video in sequence to process. This should not happen if `ordered_vimeo_videos` is not empty.", "CRITICAL")
        log_message("="*80, "INFO")
        log_message(f"POC Cycle ABORTED. Total duration: {time.time() - cycle_start_time:.2f} seconds.", "INFO")
        log_message("="*80 + "\n", "INFO")
        return # Ensure function exits here if no video to process

    cleanup_download_directory()

    downloaded_file_path = download_video(video_to_process_this_run)

    if downloaded_file_path:
        log_message(f"Video downloaded successfully. Starting playback.", "INFO")
        # Ensure that the video_to_process_this_run.get('duration') is correct for audio playback length
        play_video(downloaded_file_path, video_to_process_this_run.get('duration'))
    else:
        log_message("Download failed. Skipping playback for this cycle.", "WARNING")

    log_message("\n" + "="*80, "INFO")
    if downloaded_file_path:
        log_message(f"POC Cycle Summary: Successfully processed, downloaded, and attempted to play:", "INFO")
        log_message(f"  - Video Title: '{video_to_process_this_run['title']}'", "INFO")
        log_message(f"  - Video ID: {video_to_process_this_run['id']}", "INFO")
        log_message(f"  - File Path: {downloaded_file_path}", "INFO")
        log_message(f"  - Next video for subsequent run will be at sequence index: {(current_video_index + 1) % len(ordered_vimeo_videos)}", "INFO")
    else:
        log_message("POC Cycle Summary: No new Vimeo asset was downloaded or initiated for playback in this run.", "WARNING")
        log_message("  - Check logs for specific errors during Vimeo API calls or download attempts.", "INFO")

    log_message(f"POC Cycle FINISHED. Total duration: {time.time() - cycle_start_time:.2f} seconds.", "INFO")
    log_message("="*80 + "\n", "INFO")

if __name__ == "__main__":
    # Initialize cycle_start_time before the loop
    cycle_start_time = time.time() # Define globally for the main script scope

    # --- Main Loop for Continuous Execution ---
    while True:
        try:
            run_one_cycle()
        except KeyboardInterrupt:
            log_message("\n--- Script Interrupted by User (Ctrl+C). Exiting. ---", "INFO")
            # Attempt to terminate ffplay if script is interrupted
            if current_ffplay_process and current_ffplay_process.poll() is None:
                log_message("Attempting to terminate ffplay process on script exit.", "INFO")
                current_ffplay_process.terminate()
                try:
                    current_ffplay_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    current_ffplay_process.kill()
            break
        except Exception as e:
            log_message(f"CRITICAL: An unhandled error occurred in the main loop: {e}", "CRITICAL")
            log_message("Attempting to continue after 1 minute...", "INFO")
            time.sleep(60)

        # Calculate time to sleep to maintain a consistent cycle duration
        end_of_cycle_execution_time = time.time()
        elapsed_this_cycle_excluding_sleep = end_of_cycle_execution_time - cycle_start_time
        time_to_sleep = max(0, TARGET_CYCLE_DURATION_SECONDS - elapsed_this_cycle_excluding_sleep)

        log_message(f"Execution time this cycle: {elapsed_this_cycle_excluding_sleep:.2f}s. Waiting for {time_to_sleep:.2f} seconds until next cycle...", "INFO")
        time.sleep(time_to_sleep)
