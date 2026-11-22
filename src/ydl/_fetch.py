"""Bounded HTTPS fetch worker used by the managed release installer."""

import sys
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit


class HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("release redirect must use HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main():
    url, limit = sys.argv[1], int(sys.argv[2])
    if urlsplit(url).scheme != "https":
        raise ValueError("release URL must use HTTPS")
    opener = build_opener(HTTPSHandler(), HTTPSRedirect())
    request = Request(url, headers={"User-Agent": "ydl", "Accept": "*/*"})
    with opener.open(request, timeout=20) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"release response exceeds {limit} bytes")
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
