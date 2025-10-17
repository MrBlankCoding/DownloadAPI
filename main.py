from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
import yt_dlp
import os
import logging
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import tempfile
from pathlib import Path

# Lifespan context manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up application...")
    yield
    # Shutdown
    logger.info("Shutting down application...")
    executor.shutdown(wait=True)

app = FastAPI(lifespan=lifespan)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
MAX_WORKERS = 4
MAX_FILE_AGE_HOURS = 24  # Clean up old files after 24 hours
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "www.youtube.com_cookies.txt")

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

def cleanup_file(file_path: str):
    """Background task to delete file after response is sent"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Deleted file: {file_path}")
            
            # Also clean up any thumbnail files
            base_path = os.path.splitext(file_path)[0]
            for ext in ['.jpg', '.png', '.webp']:
                thumb_path = f"{base_path}{ext}"
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                    logger.info(f"Deleted thumbnail: {thumb_path}")
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {e}")

def cleanup_old_files():
    """Clean up files older than MAX_FILE_AGE_HOURS"""
    try:
        import time
        current_time = time.time()
        max_age_seconds = MAX_FILE_AGE_HOURS * 3600
        
        for filename in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.isfile(file_path):
                file_age = current_time - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    os.remove(file_path)
                    logger.info(f"Cleaned up old file: {filename}")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

def get_optimized_ydl_opts(video_id: str, include_progress_hook=None):
    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            },
            {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            },
        ],
        
        # Download thumbnail for embedding
        'writethumbnail': True,
        
        # Output template
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
        
        # Performance optimizations
        'noplaylist': True,
        'nocheckcertificate': True,
        'no_warnings': True,
        'quiet': False,
        'no_color': True,
        
        # Network optimizations
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'concurrent_fragment_downloads': 5,
        
        # Anti-throttling measures
        'sleep_interval': 0,
        'max_sleep_interval': 0,
        'sleep_interval_requests': 0,
        'sleep_interval_subtitles': 0,
        
        # Bypass throttling
        'http_chunk_size': 10485760,  # 10MB chunks
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'player_skip': ['webpage'],
            }
        },
        
        # Headers to avoid detection
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        
        # Cookie support - use cookies file for authentication
        'cookiefile': COOKIES_FILE if os.path.exists(COOKIES_FILE) else None,
        
        # Cache for faster repeated requests
        'no_cache_dir': False,
        
        # Skip unnecessary operations
        'skip_download': False,
        'extract_flat': False,
        'simulate': False,
    }
    
    if include_progress_hook:
        opts['progress_hooks'] = [include_progress_hook]
    
    return opts

@app.get("/download-progress/{video_id}")
async def download_audio_progress(video_id: str, background_tasks: BackgroundTasks):
    """Stream download progress using Server-Sent Events, then delete file"""
    logger.info(f"Received download request for video_id: {video_id}")
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    
    # Clean up old files before starting new download
    cleanup_old_files()
    
    async def event_generator():
        progress_data = {'status': 'starting', 'downloaded_bytes': 0, 'total_bytes': 0, 'speed': 0, 'eta': 0}
        downloaded_file = None
        
        def progress_hook(d):
            nonlocal progress_data
            if d['status'] == 'downloading':
                progress_data = {
                    'status': 'downloading',
                    'downloaded_bytes': d.get('downloaded_bytes', 0),
                    'total_bytes': d.get('total_bytes', 0) or d.get('total_bytes_estimate', 0),
                    'speed': d.get('speed', 0),
                    'eta': d.get('eta', 0),
                    'percent': d.get('_percent_str', '0%').strip(),
                }
            elif d['status'] == 'finished':
                progress_data = {
                    'status': 'finished',
                    'downloaded_bytes': d.get('downloaded_bytes', 0),
                    'total_bytes': d.get('total_bytes', 0),
                }
        
        ydl_opts = get_optimized_ydl_opts(video_id, include_progress_hook=progress_hook)

        try:
            loop = asyncio.get_event_loop()
            
            def download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info_dict = ydl.extract_info(video_url, download=True)
                    return info_dict
            
            download_task = loop.run_in_executor(executor, download)
            
            while not download_task.done():
                data = json.dumps(progress_data)
                yield f"data: {data}\n\n"
                await asyncio.sleep(0.3)
            
            info_dict = await download_task
            video_title = info_dict.get('title', 'untitled')
            expected_mp3_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
            
            if os.path.exists(expected_mp3_path):
                downloaded_file = expected_mp3_path
                logger.info(f"Successfully downloaded and converted to {expected_mp3_path}")
                final_data = json.dumps({
                    'status': 'completed',
                    'title': video_title,
                    'path': expected_mp3_path,
                    'download_url': f"/download-file/{video_id}"
                })
                yield f"data: {final_data}\n\n"
            else:
                for ext in ['.webm', '.m4a', '.ogg', '.opus']:
                    potential_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}.mp3")
                    if os.path.exists(potential_path):
                        downloaded_file = potential_path
                        logger.info(f"Successfully downloaded and converted to {potential_path}")
                        final_data = json.dumps({
                            'status': 'completed',
                            'title': video_title,
                            'path': potential_path,
                            'download_url': f"/download-file/{video_id}"
                        })
                        yield f"data: {final_data}\n\n"
                        return
                
                logger.error(f"MP3 file not found after download: {expected_mp3_path}")
                error_data = json.dumps({
                    'status': 'error',
                    'message': 'Failed to find the downloaded audio file.'
                })
                yield f"data: {error_data}\n\n"

        except Exception as e:
            logger.error(f"Error downloading video: {e}", exc_info=True)
            error_data = json.dumps({
                'status': 'error',
                'message': str(e)
            })
            yield f"data: {error_data}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@app.get("/download-file/{video_id}")
async def download_file(video_id: str, background_tasks: BackgroundTasks):
    """Download the file and delete it after sending"""
    expected_mp3_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    
    if os.path.exists(expected_mp3_path):
        # Schedule file deletion after response is sent
        background_tasks.add_task(cleanup_file, expected_mp3_path)
        return FileResponse(
            expected_mp3_path,
            media_type='audio/mpeg',
            filename=f"{video_id}.mp3"
        )
    
    # Check alternative paths
    for ext in ['.webm', '.m4a', '.ogg', '.opus']:
        potential_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}.mp3")
        if os.path.exists(potential_path):
            background_tasks.add_task(cleanup_file, potential_path)
            return FileResponse(
                potential_path,
                media_type='audio/mpeg',
                filename=f"{video_id}.mp3"
            )
    
    raise HTTPException(status_code=404, detail="File not found")

@app.api_route("/download/", methods=["GET", "POST"])
async def download_audio(video_id: str, background_tasks: BackgroundTasks):
    """Download audio and delete file after sending"""
    logger.info(f"Received download request for video_id: {video_id}")
    
    # Clean up old files periodically
    cleanup_old_files()
    
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    expected_mp3_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    
    # Check if file already exists
    if os.path.exists(expected_mp3_path):
        logger.info(f"File already exists, serving: {expected_mp3_path}")
        background_tasks.add_task(cleanup_file, expected_mp3_path)
        return FileResponse(
            expected_mp3_path,
            media_type='audio/mpeg',
            filename=f"{video_id}.mp3"
        )
    
    ydl_opts = get_optimized_ydl_opts(video_id)

    try:
        loop = asyncio.get_event_loop()
        
        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(video_url, download=True)
                return info_dict
        
        info_dict = await loop.run_in_executor(executor, download)
        video_title = info_dict.get('title', 'untitled')

        if os.path.exists(expected_mp3_path):
            logger.info(f"Successfully downloaded and converted to {expected_mp3_path}")
            background_tasks.add_task(cleanup_file, expected_mp3_path)
            return FileResponse(
                expected_mp3_path,
                media_type='audio/mpeg',
                filename=f"{video_title}.mp3"
            )
        else:
            for ext in ['.webm', '.m4a', '.ogg', '.opus']:
                potential_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}.mp3")
                if os.path.exists(potential_path):
                    logger.info(f"Successfully downloaded and converted to {potential_path}")
                    background_tasks.add_task(cleanup_file, potential_path)
                    return FileResponse(
                        potential_path,
                        media_type='audio/mpeg',
                        filename=f"{video_title}.mp3"
                    )

            logger.error(f"MP3 file not found after download: {expected_mp3_path}")
            raise HTTPException(status_code=500, detail="Failed to find the downloaded audio file.")

    except Exception as e:
        logger.error(f"Error downloading video: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/video-info/{video_id}")
async def get_video_info(video_id: str):
    """Get video metadata without downloading"""
    logger.info(f"Fetching info for video_id: {video_id}")
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    
    try:
        loop = asyncio.get_event_loop()
        
        def extract_info():
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'skip_download': True,
                'cookiefile': COOKIES_FILE if os.path.exists(COOKIES_FILE) else None,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['android', 'web'],
                        'player_skip': ['webpage'],
                    }
                },
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(video_url, download=False)
        
        info_dict = await loop.run_in_executor(executor, extract_info)
        
        thumbnail_url = None
        thumbnails = info_dict.get('thumbnails', [])
        if thumbnails:
            thumbnail_url = thumbnails[-1].get('url', '')
        
        return {
            'video_id': video_id,
            'title': info_dict.get('title', 'Unknown'),
            'artist': info_dict.get('artist') or info_dict.get('uploader', 'Unknown'),
            'channel': info_dict.get('channel', 'Unknown'),
            'thumbnail_url': thumbnail_url,
            'duration': info_dict.get('duration', 0),
        }
        
    except Exception as e:
        logger.error(f"Error fetching video info: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cleanup")
async def manual_cleanup():
    """Manual endpoint to trigger cleanup of old files"""
    try:
        cleanup_old_files()
        return {"message": "Cleanup completed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    return {
        "status": "healthy",
        "download_dir": DOWNLOAD_DIR,
        "max_workers": MAX_WORKERS
    }

@app.get("/")
def read_root():
    return {"message": "Welcome to the Optimized Music Downloader API"}