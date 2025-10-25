   # YouTube Download API

A FastAPI-based service for downloading YouTube videos as MP3 files with progress tracking.

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure YouTube Authentication

YouTube requires authentication to bypass bot detection. You need:

#### A. Cookie File (Required)

1. Install a browser extension to export cookies:
   - Chrome/Edge: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - Firefox: [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)

2. Go to [YouTube](https://www.youtube.com) and sign in

3. Export cookies for `youtube.com` and save as `www.youtube.com_cookies.txt` in this directory

#### B. PO Token & Visitor Data (Highly Recommended)

YouTube now requires these tokens for cookie-based authentication:

1. **Open YouTube in your browser** (while logged in)

2. **Open Developer Console** (F12 or Right-click → Inspect)

3. **Go to Console tab** and paste this code:

```javascript
// Extract po_token and visitor_data
const ytcfg = window.ytcfg?.data_ || {};
const poToken = ytcfg.ID_TOKEN || ytcfg.PO_TOKEN || 'Not found';
const visitorData = ytcfg.VISITOR_DATA || ytcfg.INNERTUBE_CONTEXT?.client?.visitorData || 'Not found';

console.log('PO_TOKEN:', poToken);
console.log('VISITOR_DATA:', visitorData);

// Copy to clipboard
copy(`export YT_PO_TOKEN="${poToken}"\nexport YT_VISITOR_DATA="${visitorData}"`);
console.log('\n✅ Environment variables copied to clipboard!');
```

4. **The tokens will be copied to your clipboard**. They look like:
   - `PO_TOKEN`: Long string (100+ characters)
   - `VISITOR_DATA`: Shorter string (~20-30 characters)

5. **Set environment variables**:

```bash
export YT_PO_TOKEN="your_po_token_here"
export YT_VISITOR_DATA="your_visitor_data_here"
```

### 3. Start the Server

#### Local Development

```bash
uvicorn main:app --reload --port 8000
```

#### Docker

```bash
# Build
docker build -t youtube-api .

# Run with environment variables
docker run -p 8080:8080 \
  -e YT_PO_TOKEN="your_po_token" \
  -e YT_VISITOR_DATA="your_visitor_data" \
  youtube-api
```

## API Endpoints

- `GET /video-info/{video_id}` - Get video metadata
- `GET /download-progress/{video_id}` - Download with SSE progress updates
- `GET /download-file/{video_id}` - Download the converted MP3 file
- `GET /health` - Health check
- `GET /cleanup` - Manually trigger old file cleanup

## Troubleshooting

### "Sign in to confirm you're not a bot" Error

This means YouTube is blocking the request. Try:

1. **Refresh your cookies** - Export fresh cookies from your browser
2. **Get new tokens** - Extract fresh `PO_TOKEN` and `VISITOR_DATA` using the script above
3. **Use the same IP** - If possible, run the container from the same network/IP where you exported cookies
4. **Check cookie expiration** - Cookies typically expire after a few months

### Cookies Expire Quickly

YouTube cookies can expire or become invalid if:
- You log out of YouTube
- You change your password
- YouTube detects unusual activity
- The IP address changes significantly

**Solution**: Re-export cookies and tokens regularly (weekly recommended for production)

### How to Verify Tokens Are Working

Check the startup logs:

```
INFO:main:Cookie file found at: /app/www.youtube.com_cookies.txt
INFO:main:Cookie file size: 3228 bytes
INFO:main:PO_TOKEN configured (length: 152)
INFO:main:VISITOR_DATA configured (length: 24)
```

If you see warnings about missing tokens, set the environment variables.
