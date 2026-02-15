"""
HTTP Stream Reader - Thread-based HTTP stream reader that writes to a pipe.
This allows us to use the same fetch_chunk() path for both transcode and HTTP streams.
"""

import os
import socket
import threading

import requests
from requests.adapters import HTTPAdapter

from .utils import get_logger

logger = get_logger()


class HTTPStreamReader:
    """Thread-based HTTP stream reader that writes to a pipe"""

    def __init__(self, url, user_agent=None, chunk_size=8192):
        self.url = url
        self.user_agent = user_agent
        self.chunk_size = chunk_size
        self.session = None
        self.response = None
        self.thread = None
        self.pipe_read = None
        self.pipe_write = None
        self.running = False
        # Set to True when a DNS resolution failure is detected from the
        # actual connection attempt (socket.gaierror inside ConnectionError).
        self.dns_failure = False

    def start(self):
        """Start the HTTP stream reader thread"""
        # Create a pipe (works on Windows and Unix)
        self.pipe_read, self.pipe_write = os.pipe()

        # Start the reader thread
        self.running = True
        self.thread = threading.Thread(target=self._read_stream, daemon=True)
        self.thread.start()

        logger.info(f"Started HTTP stream reader thread for {self.url}")
        return self.pipe_read

    def _read_stream(self):
        """Thread worker that reads HTTP stream and writes to pipe"""
        try:
            # Build headers
            headers = {}
            if self.user_agent:
                headers["User-Agent"] = self.user_agent

            logger.info(f"HTTP reader connecting to {self.url}")

            # Create session
            self.session = requests.Session()

            # Disable retries for faster failure detection
            adapter = HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

            # Stream the URL
            self.response = self.session.get(
                self.url,
                headers=headers,
                stream=True,
                timeout=(5, 30),  # 5s connect, 30s read
            )

            if self.response.status_code != 200:
                logger.error(f"HTTP {self.response.status_code} from {self.url}")
                return

            logger.info(f"HTTP reader connected successfully, streaming data...")

            # Stream chunks to pipe
            chunk_count = 0
            for chunk in self.response.iter_content(chunk_size=self.chunk_size):
                if not self.running:
                    break

                if chunk:
                    try:
                        # Write binary data to pipe
                        os.write(self.pipe_write, chunk)
                        chunk_count += 1

                        # Log progress periodically
                        if chunk_count % 1000 == 0:
                            logger.debug(f"HTTP reader streamed {chunk_count} chunks")
                    except OSError as e:
                        logger.error(f"Pipe write error: {e}")
                        break

            logger.info("HTTP stream ended")

        except requests.exceptions.ConnectionError as e:
            if _is_dns_error(e):
                self.dns_failure = True
                logger.error(f"HTTP reader DNS resolution failed for {self.url}: {e}")
            else:
                logger.error(f"HTTP reader connection error: {e}")
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP reader request error: {e}")
        except Exception as e:
            logger.error(f"HTTP reader unexpected error: {e}", exc_info=True)
        finally:
            self.running = False
            # Close write end of pipe to signal EOF
            try:
                if self.pipe_write is not None:
                    os.close(self.pipe_write)
                    self.pipe_write = None
            except:
                pass

    def stop(self):
        """Stop the HTTP stream reader"""
        logger.info("Stopping HTTP stream reader")
        self.running = False

        # Close response
        if self.response:
            try:
                self.response.close()
            except:
                pass

        # Close session
        if self.session:
            try:
                self.session.close()
            except:
                pass

        # Close write end of pipe
        if self.pipe_write is not None:
            try:
                os.close(self.pipe_write)
                self.pipe_write = None
            except:
                pass

        # Wait for thread
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# DNS-failure detection helpers
# ---------------------------------------------------------------------------

# Patterns that appear in stderr output from ffmpeg, vlc, streamlink, etc.
# when DNS resolution fails.  Used by StreamManager._log_stderr_content().
DNS_ERROR_PATTERNS = (
    "name or service not known",
    "temporary failure in name resolution",
    "no address associated with hostname",
    "could not resolve host",
    "could not resolve hostname",
    "getaddrinfo failed",
    "nodename nor servname provided",
    "server name not resolved",
    "name resolution failed",
    "dns_error",
    # VLC-specific
    "resolution of host",
)


def _is_dns_error(exc: Exception) -> bool:
    """
    Return ``True`` if *exc* (typically a ``requests.ConnectionError``)
    wraps a DNS resolution failure (``socket.gaierror``).

    Checks both the exception chain (``__cause__``) and the string
    representation for well-known DNS error phrases.
    """
    # Walk the exception chain looking for socket.gaierror
    cause = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return True
        cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
        if cause is exc:
            break  # prevent infinite loop on circular references

    # Fallback: check the stringified exception for DNS-related phrases
    exc_str = str(exc).lower()
    return any(pattern in exc_str for pattern in DNS_ERROR_PATTERNS)


def is_dns_error_in_text(text: str) -> bool:
    """
    Return ``True`` if *text* (e.g. a stderr line from ffmpeg/vlc) contains
    a phrase that indicates a DNS resolution failure.
    """
    text_lower = text.lower()
    return any(pattern in text_lower for pattern in DNS_ERROR_PATTERNS)
