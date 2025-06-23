# digital_signage_controller.py - for general use
# make sure you repurpose it accordingly for Pi OS


import os
import requests
import time
from datetime import datetime, UTC # Import UTC for timezone-aware datetime
import json
import re # For regular expressions, used in sorting video names
from html.parser import HTMLParser # For parsing HTML (e.g., Vimeo embed code)
import urllib.parse # For URL parsing and encoding
import subprocess # For running external commands (like ffplay)
import signal # For sending termination signals to child processes
import sys # For potential redirection of stdout/stderr if needed for debugging

# --- User Configuration ---
# This is the base directory where video files and log files will be stored.
# IMPORTANT: Replace with an actual, accessible path on your system.
# For macOS example: '/Users/YourUsername/Documents/digital_signage_media'
# For Raspberry Pi example: '/home/pi/digital_signage_media'
DOWNLOAD_DIR = '/path/to/your/signage_folder' 

# Vimeo API Credentials
# Obtain your Personal Access Token from https://developer.vimeo.com/apps
# Ensure your token has 'public', 'private', and 'video_files' scopes enabled.
VIMEO_ACCESS_TOKEN = "YOUR_VIMEO_ACCESS_TOKEN_HERE" 

# Vimeo Project (Folder/Album) ID
# This is the numerical ID of the Vimeo Project/Album that contains your video assets.
# You can find this in the URL of your Vimeo project page (e.g., vimeo.com/manage/videos/project/123456789)
VIMEO_PROJECT_ID = "YOUR_VIMEO_PROJECT_ID_HERE" 

# File to store the index of the the last played video to maintain playback order across runs.
LAST_PLAYED_INDEX_FILE = os.path.join(DOWNLOAD_DIR, "last_played_index.json")

# Path for the main log file.
LOG_FILE = os.path.join(DOWNLOAD_DIR, "system_log.log")

# --- System Control Configuration ---
# This is the target fixed duration for each complete cycle (download + play + wait).
# E.g., 120 seconds for a 2-minute cycle. The script will wait to ensure this cycle duration.
TARGET_CYCLE_DURATION_SECONDS = 120 # (2 minutes)

# --- Playback Configuration ---
# Set to a number (e.g., 30) to play each video for a fixed duration.
# If set to 'None', the script will attempt to play the video's full duration (as reported by Vimeo API).
# Note: The video will be cut off if its full duration exceeds the TARGET_CYCLE_DURATION_SECONDS.
PLAY_DURATION_SECONDS = None 

# This is the absolute minimum time (in seconds) the video player will attempt to run.
# This ensures visibility even if the Vimeo API reports a duration of 0 or a very short time.
MIN_PLAY_DURATION_SECONDS = 5 

# --- Vimeo API Base URL and Headers ---
VIMEO_API_BASE_URL = "https://api.vimeo.com"
VIMEO_HEADERS = {
    "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}",
    "Accept": "application/vnd.vimeo.*+json;version=3.4" # Requesting API version 3.4
}

# --- Global variable to track the currently playing ffplay process ---
# This allows the script to stop a playing video from a previous cycle.
current_ffplay_process = None 


# --- HTML Parser for Extracting Iframe Source (Internal Use) ---
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


# --- Logging Function ---
def log_message(message, level="INFO"):
    """
    Logs messages to a file and prints them to the console.
    Levels: INFO, DEBUG, WARNING, ERROR, CRITICAL.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {level.upper()}: {message}"
    
    print(log_entry) # Always print to console for real-time feedback
    
    try:
        # Ensure the directory for the log file exists
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f: # Append to the log file
            f.write(log_entry + "\n")
    except Exception as e:
        # Fallback print if logging to file fails (e.g., permission issues)
        print(f"[{timestamp}] ERROR: Failed to write to log file '{LOG_FILE}': {e}")

# --- Directory Cleanup Function ---
def cleanup_download_directory():
    """Deletes all .mp4 files in the designated download directory."""
    log_message(f"STARTING CLEANUP for '{DOWNLOAD_DIR}'", "DEBUG")
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        log_message(f"Created missing download directory: '{DOWNLOAD_DIR}'", "INFO")
        return

    files_deleted_count = 0
    for filename in os.listdir(DOWNLOAD_DIR):
        if filename.lower().endswith(".mp4"): # Case-insensitive check
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            try:
                os.remove(filepath)
                files_deleted_count += 1
                log_message(f"Deleted file: '{filename}'", "INFO")
            except OSError as e:
                log_message(f"Failed to delete '{filename}': {e}", "ERROR")
    
    if files_deleted_count > 0:
        log_message(f"Cleanup complete. Deleted {files_deleted_count} MP4 files.", "INFO")
    else:
        log_message("Cleanup complete. No MP4 files found to delete.", "INFO")

# --- Vimeo Video Fetching Logic ---
def get_all_vimeo_videos_in_order():
    """
    Fetches all videos from the specified Vimeo Project/Album,
    sorts them numerically by 'BLACKLIVES_BL_XXX' name,
    and tries to find a suitable MP4 download link.
    """
    log_message("Fetching all videos from Vimeo project...", "INFO")
    all_videos_data = []
    
    # API endpoint to list videos within a specific project/album
    current_page_url = f"{VIMEO_API_BASE_URL}/me/projects/{VIMEO_PROJECT_ID}/videos"
    
    try:
        while current_page_url:
            log_message(f"Requesting Vimeo API page: {current_page_url}", "DEBUG")
            response = requests.get(
                current_page_url,
                headers=VIMEO_HEADERS,
                params={
                    'per_page': 100, # Request maximum allowed per page to minimize API calls
                    # Request all relevant fields: ID, name, files (for direct links), 
                    # download (for expiring links), embed.html (for player links), and duration.
                    'fields': 'uri,name,files,download,embed.html,duration' 
                },
                timeout=30, # Timeout for the API request
                verify=False # WARNING: Temporarily disables SSL certificate verification for POC.
                             # RE-ENABLE/FIX IN PRODUCTION.
            )

            response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)

            data = response.json()
            all_videos_data.extend(data.get('data', []))
            current_page_url = data.get('paging', {}).get('next') # Get URL for the next page, or None if last page
        
        if not all_videos_data:
            log_message("No videos found in the specified Vimeo Project. Check Project ID and API token permissions.", "WARNING")
            return []

        downloadable_videos_info = []
        iframe_parser = IframeSrcExtractor() # Re-use parser instance

        # Helper to find the best MP4 link from a list of file objects (e.g., from 'files' or 'download' arrays)
        def find_best_mp4_link_in_array(file_list):
            best = None
            for f in file_list:
                # Prioritize 'video/mp4' type, ensure it has a link, and choose highest resolution
                if f.get('type') == 'video/mp4' and f.get('link') and f.get('width') is not None and f.get('height') is not None:
                    if best is None or f['width'] * f['height'] > best['width'] * best['height']:
                        best = f
            return best
        
        for video in all_videos_data:
            selected_link_url = None
            link_source_type = "None"
            selected_quality_info = None # To store the selected file object (with quality, width, size, etc.)

            log_message(f"--- Processing Video '{video.get('name', 'N/A')}' (ID: {video['uri'].split('/')[-1]}) ---", "DEBUG")
            log_message(f"  Raw Duration from API: {video.get('duration', 'N/A')}", "DEBUG")

            # --- Link Search Priority ---
            # 1. PRIORITY: Try to find the best MP4 in the 'files' field. These links are typically non-expiring and direct.
            if 'files' in video and isinstance(video['files'], list):
                found_file = find_best_mp4_link_in_array(video['files'])
                if found_file:
                    selected_quality_info = found_file
                    selected_link_url = found_file['link']
                    link_source_type = "files"
                    log_message(f"  Found BEST link in 'files' field: {found_file.get('quality', 'N/A')} - {found_file['link']}", "DEBUG")
            
            # 2. FALLBACK: If no suitable file in 'files', try the 'download' field. These links are typically expiring (24h TTL).
            if selected_link_url is None and 'download' in video and isinstance(video['download'], list):
                found_download = find_best_mp4_link_in_array(video['download'])
                if found_download:
                    selected_quality_info = found_download
                    selected_link_url = found_download['link']
                    link_source_type = "download"
                    log_message(f"  Found BEST link in 'download' field: {found_download.get('quality', 'N/A')} - {found_download['link']}", "DEBUG")

            # 3. LAST RESORT: Extract from 'embed.html' if no direct file links found in 'files' or 'download'.
            # Note: Downloading from these player URLs (e.g., player.vimeo.com/video/{ID})
            # often results in a 403 Forbidden error, as they are for embedding/streaming, not direct downloads.
            if selected_link_url is None and 'embed' in video and 'html' in video['embed']:
                embed_html = video['embed']['html']
                extracted_src = iframe_parser.get_src(embed_html)
                if extracted_src:
                    parsed_url = urllib.parse.urlparse(extracted_src)
                    query_params = urllib.parse.parse_qs(parsed_url.query)
                    query_params['autoplay'] = ['1'] 
                    reconstructed_url = urllib.parse.urlunparse(
                        parsed_url._replace(query=urllib.parse.urlencode(query_params, doseq=True))
                    )
                    selected_link_url = reconstructed_url
                    link_source_type = "embed_html"
                    log_message(f"  Extracted embed link (likely 403 if downloaded directly): {selected_link_url}", "DEBUG")

            # If a usable link was found (from any source)
            if selected_link_url:
                log_message(f"Selected '{link_source_type}' link for: '{video['name']}'", "INFO")
                downloadable_videos_info.append({
                    'id': video['uri'].split('/')[-1],
                    'title': video['name'],
                    'download_link': selected_link_url,
                    # Provide actual quality/size if found, else 'N/A'
                    'quality': selected_quality_info.get('quality', 'N/A') if selected_quality_info else 'N/A',
                    'size': selected_quality_info.get('size', 'N/A') if selected_quality_info else 'N/A',
                    'created_time': video.get('created_time', '1970-01-01T00:00:00+00:00'), # Default date if missing
                    'duration': video.get('duration', 0) # Default to 0 if missing
                })
            else:
                log_message(f"NO USABLE MP4 LINK FOUND (from files/download/embed) for: '{video.get('name', 'Unknown')}' (ID: {video['uri'].split('/')[-1]}).", "WARNING")
                log_message(f"  This is often due to Vimeo account tier limitations or specific video privacy settings.", "WARNING")
                # For debugging, you could uncomment these to see raw API response parts
                # log_message(f"  Raw 'files' data: {video.get('files', 'Not present')}", "DEBUG")
                # log_message(f"  Raw 'download' data: {video.get('download', 'Not present')}", "DEBUG")


        # Sort the videos numerically based on the 'BL_XXX' part of their name
        def get_bl_number(video_name):
            match = re.search(r'BL_(\d+)', video_name)
            return int(match.group(1)) if match else float('inf') # Put unmatchable names at the end

        downloadable_videos_info.sort(key=lambda x: get_bl_number(x['title']))
        
        log_message(f"Successfully fetched and sorted {len(downloadable_videos_info)} potentially downloadable videos.", "INFO")
        return downloadable_videos_info

    except requests.exceptions.RequestException as e:
        log_message(f"HTTP/Network error during Vimeo API call: {e}", "ERROR")
        log_message(f"  Ensure your internet connection is stable and the API token is correct.", "INFO")
        return []
    except json.JSONDecodeError as e:
        log_message(f"Error decoding JSON response from Vimeo API: {e}. The response might be invalid.", "ERROR")
        return []
    except Exception as e:
        log_message(f"An unexpected error occurred during Vimeo API video fetching/sorting: {e}", "CRITICAL")
        return []

# --- Sequence Management ---
def get_next_video_in_sequence(all_videos):
    """
    Determines the next video to download based on the last played index.
    Updates the index file for the next run.
    """
    log_message("Determining next video in sequence...", "INFO")
    if not all_videos:
        log_message("No videos available to determine next in sequence.", "WARNING")
        return None, 0

    current_index = 0
    if os.path.exists(LAST_PLAYED_INDEX_FILE):
        try:
            with open(LAST_PLAYED_INDEX_FILE, 'r') as f:
                data = json.load(f)
                current_index = data.get('last_index', 0)
                log_message(f"Loaded last_index: {current_index} from '{LAST_PLAYED_INDEX_FILE}'", "DEBUG")
        except json.JSONDecodeError:
            log_message("Error reading last_played_index.json. File corrupted. Resetting index to 0.", "ERROR")
            current_index = 0
        except Exception as e:
            log_message(f"Unexpected error loading '{LAST_PLAYED_INDEX_FILE}': {e}. Resetting index to 0.", "ERROR")
            current_index = 0

    # Ensure current_index is within the bounds of the fetched video list
    if not (0 <= current_index < len(all_videos)):
        log_message(f"Stored index {current_index} is out of bounds for {len(all_videos)} videos. Resetting to 0 (will start from the first video).", "WARNING")
        current_index = 0

    video_for_this_run = all_videos[current_index]
    next_index_for_save = (current_index + 1) % len(all_videos) # Calculate next index, wraps around

    try:
        with open(LAST_PLAYED_INDEX_FILE, 'w') as f:
            json.dump({'last_index': next_index_for_save}, f)
        log_message(f"Saved next index for the following run: {next_index_for_save}.", "INFO")
    except Exception as e:
        log_message(f"Error saving next index to '{LAST_PLAYED_INDEX_FILE}': {e}", "ERROR")

    log_message(f"Determined video for current run: '{video_for_this_run['title']}' (Index: {current_index})", "INFO")
    return video_for_this_run, current_index

# --- Video Download Logic ---
def download_video(video_info):
    """Downloads a video file from a given URL to the designated directory."""
    # Create a safe filename from the video title and ID
    safe_title = "".join([c for c in video_info['title'] if c.isalnum() or c in (' ', '_', '-')]).strip()
    safe_title = safe_title.replace(' ', '_')
    filename = f"{video_info['id']}_{safe_title}.mp4" 
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        log_message(f"Video '{video_info['title']}' already exists locally and is not empty. Skipping download.", "INFO")
        return filepath

    log_message(f"Attempting to download '{video_info['title']}' from Vimeo URL: {video_info['download_link']}", "INFO")
    try:
        # requests will automatically follow HTTP 302 redirects (e.g., from player.vimeo.com to CDN).
        # Custom headers are added to make the request appear more like a web browser.
        custom_headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://player.vimeo.com/' # Pretend the request came from a Vimeo player page
        }
        response = requests.get(
            video_info['download_link'], 
            stream=True, # Stream content in chunks for efficiency with large files
            timeout=300, # Max 5 minutes for the download request to complete
            verify=False, # WARNING: Disables SSL verification - REMOVE IN PRODUCTION!
            headers=custom_headers, 
            allow_redirects=True
        )
        response.raise_for_status() # Raise an exception for HTTP error codes (4xx or 5xx)

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192): # Iterate over response content in 8KB chunks
                f.write(chunk)
        log_message(f"SUCCESS: Downloaded '{video_info['title']}' to '{filepath}'", "INFO")
        return filepath
    except requests.exceptions.RequestException as e:
        log_message(f"ERROR: Download failed for '{video_info['title']}': {e}", "ERROR")
        log_message(f"  URL: {video_info['download_link']}", "ERROR")
        log_message(f"  Reason: {e.response.status_code if e.response else 'No response status'} {e.response.reason if e.response else 'No response reason'}", "ERROR")
        if os.path.exists(filepath):
            os.remove(filepath) # Clean up partially downloaded file
        return None
    except Exception as e:
        log_message(f"CRITICAL: An unexpected error occurred during download: {e}", "CRITICAL")
        return None

# --- Video Playback Logic ---
def play_video(filepath, video_duration_seconds=None):
    """
    Plays a video using ffplay in a non-blocking way, with a controlled duration.
    It updates a global variable `current_ffplay_process` to allow external control.
    Args:
        filepath (str): Path to the video file to play.
        video_duration_seconds (int, optional): The actual duration of the video as reported by Vimeo.
                                                 Used to inform ffplay's play duration.
    Returns:
        bool: True if playback was successfully initiated, False otherwise.
    """
    global current_ffplay_process # Declare intent to modify the global process variable

    if not os.path.exists(filepath):
        log_message(f"Error: Video file not found for playback: '{filepath}'", "ERROR")
        return False

    # Base ffplay command-line arguments
    ffplay_command = [
        "ffplay",
        "-fs",              # Fullscreen mode
        "-autoexit",        # Automatically exit when playback finishes (if not terminated externally)
        "-hide_banner",     # Suppress FFmpeg's large startup banner
        "-nostats",         # Don't show playback statistics overlay
        "-loglevel", "info", # Show basic info/error messages from ffplay itself
        filepath # The video file to play
    ]

    # --- Determine the effective playback duration for FFplay ---
    # Start with the user-defined PLAY_DURATION_SECONDS if set, otherwise use API duration.
    effective_play_duration = PLAY_DURATION_SECONDS 
    
    if effective_play_duration is None and video_duration_seconds is not None and video_duration_seconds > 0:
        # If no fixed play duration, use the API's reported duration
        effective_play_duration = video_duration_seconds
    
    # Ensure a minimum playback duration for visibility, especially if API duration is 0 or less.
    if effective_play_duration is None or effective_play_duration <= 0:
        effective_play_duration = MIN_PLAY_DURATION_SECONDS 

    # Add the '-t' (duration) flag to ffplay command if a valid duration is determined
    if effective_play_duration > 0: # Ensure positive duration
        ffplay_command.insert(-1, "-t") 
        ffplay_command.insert(-1, str(effective_play_duration)) 
        log_message(f"Playing for approx. {effective_play_duration:.2f} seconds (API duration: {video_duration_seconds}s).", "INFO")
    else: 
        log_message(f"Playing full video (API duration: {video_duration_seconds if video_duration_seconds is not None else 'N/A'}s).", "INFO")


    log_message(f"Starting playback with ffplay: {' '.join(ffplay_command)}", "INFO")
    try:
        # Start ffplay process in the background (non-blocking).
        # stdout/stderr are redirected to DEVNULL to keep the main script's terminal clean.
        # The process object is stored globally to allow it to be terminated later.
        current_ffplay_process = subprocess.Popen(ffplay_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) 
        log_message(f"FFplay process started with PID: {current_ffplay_process.pid}", "INFO")
        
        # The script does NOT wait here; it continues its execution immediately.
        return True
    except FileNotFoundError:
        log_message("Error: 'ffplay' command not found. Please ensure FFmpeg is installed and in your system's PATH.", "CRITICAL")
        log_message("  For macOS (Homebrew): 'brew install ffmpeg'", "INFO")
        log_message("  For Raspberry Pi (Debian/Ubuntu): 'sudo apt update && sudo apt install ffmpeg'", "INFO")
        return False
    except Exception as e:
        log_message(f"An unexpected error occurred during ffplay playback initiation for '{filepath}': {e}", "CRITICAL")
        return False

# --- Main Cycle Execution Function ---
def run_one_cycle():
    """
    Executes a single cycle of the digital signage process:
    1. Terminates any previously running video player.
    2. Fetches the ordered list of videos from Vimeo.
    3. Determines the next video in sequence.
    4. Cleans the local download directory.
    5. Downloads the new video.
    6. Initiates playback of the new video.
    """
    global current_ffplay_process # Access the global variable for the player process
    global cycle_start_time # Access the global variable defined in __main__

    cycle_start_time = time.time() # Mark the start time of this specific cycle

    log_message("\n" + "="*80, "INFO")
    log_message("--- Digital Signage Script: STARTING NEW CYCLE ---", "INFO")
    log_message(f"Current System Time (UTC): {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}", "INFO") 
    log_message("="*80 + "\n", "INFO")
    
    # Ensure the download directory exists
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # --- Step 1: Take over - Kill any previous ffplay process ---
    # Check if a player process exists and if it's still running (poll() is None if running)
    if current_ffplay_process and current_ffplay_process.poll() is None: 
        log_message(f"Forcefully terminating previous ffplay process (PID: {current_ffplay_process.pid})...", "INFO")
        try:
            current_ffplay_process.terminate() # Request graceful termination
            current_ffplay_process.wait(timeout=5) # Wait up to 5 seconds for it to exit
            log_message("Previous ffplay process terminated.", "INFO")
        except subprocess.TimeoutExpired: # If it doesn't terminate gracefully
            log_message("Previous ffplay process did not terminate gracefully, killing it forcefully.", "WARNING")
            current_ffplay_process.kill() # Force kill
            current_ffplay_process.wait() # Ensure it's dead
        except Exception as e:
            log_message(f"Error terminating previous ffplay process: {e}", "ERROR")
        current_ffplay_process = None # Clear the global reference after termination

    # --- Step 2: Fetch and sort video list from Vimeo ---
    ordered_vimeo_videos = get_all_vimeo_videos_in_order()

    if not ordered_vimeo_videos:
        log_message("No videos available to process after fetching from Vimeo. Check logs for API errors or project content.", "CRITICAL")
        log_message("="*80, "INFO")
        log_message(f"Cycle ABORTED. Total duration: {time.time() - cycle_start_time:.2f} seconds.", "INFO")
        log_message("="*80 + "\n", "INFO")
        return # Abort cycle if no videos are found

    # --- Step 3: Determine the next video in sequence ---
    video_to_process_this_run, current_video_index = get_next_video_in_sequence(ordered_vimeo_videos)

    if not video_to_process_this_run:
        log_message("Could not determine next video in sequence to process. This should not happen if `ordered_vimeo_videos` is not empty.", "CRITICAL")
        log_message("="*80, "INFO")
        log_message(f"Cycle ABORTED. Total duration: {time.time() - cycle_start_time:.2f} seconds.", "INFO")
        log_message("="*80 + "\n", "INFO")
        return # Abort cycle if next video cannot be determined

    # --- Step 4: Cleanup ALL existing .mp4 files in the directory BEFORE downloading the new one ---
    cleanup_download_directory()

    # --- Step 5: Download the determined next video ---
    downloaded_file_path = download_video(video_to_process_this_run)

    # --- Step 6: Initiate playback if download was successful ---
    if downloaded_file_path:
        log_message(f"Video downloaded successfully. Starting playback.", "INFO")
        play_video(downloaded_file_path, video_to_process_this_run.get('duration')) # Pass API duration to play_video
    else:
        log_message("Download failed. Skipping playback for this cycle.", "WARNING")

    # --- Cycle Summary ---
    log_message("\n" + "="*80, "INFO")
    if downloaded_file_path:
        log_message(f"Cycle Summary: Successfully processed, downloaded, and attempted to play:", "INFO")
        log_message(f"  - Video Title: '{video_to_process_this_run['title']}'", "INFO")
        log_message(f"  - Video ID: {video_to_process_this_run['id']}", "INFO")
        log_message(f"  - File Path: '{downloaded_file_path}'", "INFO")
        log_message(f"  - Next video for subsequent run will be at sequence index: {(current_video_index + 1) % len(ordered_vimeo_videos)}", "INFO")
    else:
        log_message("Cycle Summary: No new Vimeo asset was downloaded or initiated for playback in this run.", "WARNING")
        log_message("  - Check logs for specific errors during Vimeo API calls or download attempts.", "INFO")
    
    log_message(f"Cycle FINISHED. Total duration: {time.time() - cycle_start_time:.2f} seconds.", "INFO")
    log_message("="*80 + "\n", "INFO")

# --- Main Execution Block ---
if __name__ == "__main__":
    # --- Ensure FFmpeg (which includes ffplay) is installed on your system ---
    # For macOS (using Homebrew): brew install ffmpeg
    # For Raspberry Pi (Debian/Ubuntu): sudo apt update && sudo apt install ffmpeg

    # Initialize cycle_start_time before the loop starts.
    # This variable tracks the beginning of the current cycle for duration calculation.
    cycle_start_time = time.time() 

    # --- Main Loop for Continuous Execution ---
    while True:
        try:
            run_one_cycle()
        except KeyboardInterrupt:
            # Handle graceful exit when user presses Ctrl+C
            log_message("\n--- Script Interrupted by User (Ctrl+C). Exiting. ---", "INFO")
            # Attempt to terminate ffplay if it's still running when script is interrupted
            if current_ffplay_process and current_ffplay_process.poll() is None:
                log_message("Attempting to terminate ffplay process on script exit.", "INFO")
                current_ffplay_process.terminate()
                try:
                    current_ffplay_process.wait(timeout=5) # Give it a few seconds to terminate
                except subprocess.TimeoutExpired:
                    current_ffplay_process.kill() # Force kill if it doesn't respond
            break # Exit the infinite loop

        except Exception as e:
            # Catch any unexpected errors in the main loop to prevent script from crashing
            log_message(f"CRITICAL: An unhandled error occurred in the main loop: {e}", "CRITICAL")
            log_message("Attempting to continue after 1 minute to avoid rapid crashing...", "INFO")
            time.sleep(60) # Wait a minute before trying the next cycle after a critical error
        
        # --- Calculate and apply sleep to maintain consistent cycle duration ---
        end_of_cycle_execution_time = time.time()
        # Calculate how long the current cycle's operations took (excluding the sleep at the end)
        elapsed_this_cycle_excluding_sleep = end_of_cycle_execution_time - cycle_start_time
        # Determine how long to sleep to reach the TARGET_CYCLE_DURATION_SECONDS
        time_to_sleep = max(0, TARGET_CYCLE_DURATION_SECONDS - elapsed_this_cycle_excluding_sleep)

        log_message(f"Execution time this cycle: {elapsed_this_cycle_excluding_sleep:.2f}s. Waiting for {time_to_sleep:.2f} seconds until next cycle...", "INFO")
        time.sleep(time_to_sleep) # Pause until the next cycle begins
