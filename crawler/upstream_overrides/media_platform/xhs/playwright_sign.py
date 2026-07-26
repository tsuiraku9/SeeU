# -*- coding: utf-8 -*-
# SPDX-License-Identifier: LicenseRef-MediaCrawler-NON-COMMERCIAL-LEARNING-1.1
# Copyright (c) 2025 relakkes@gmail.com
#
# Backported from NanmiCoder/MediaCrawler commit 17f66121e0fcc40fc23958b995bec873d422667d.
# It remains subject to MediaCrawler's NON-COMMERCIAL LEARNING LICENSE 1.1.

"""Xiaohongshu signature generation using xhshow's public high-level API."""

from typing import Any, Dict, Optional, Union

from .xhs_sign import get_trace_id


def sign_with_xhshow(
    uri: str,
    data: Optional[Union[Dict, str]] = None,
    cookie_str: str = "",
    method: str = "POST",
) -> Dict[str, Any]:
    """Generate the request signature with xhshow >= 0.2.0."""
    from xhshow import Xhshow

    xhshow_client = Xhshow()
    if method.upper() == "POST":
        headers = xhshow_client.sign_headers_post(
            uri=uri,
            cookies=cookie_str,
            payload=data if isinstance(data, dict) else {},
        )
    else:
        headers = xhshow_client.sign_headers_get(
            uri=uri,
            cookies=cookie_str,
            params=data if isinstance(data, dict) else {},
        )

    return {
        "x-s": headers.get("x-s", ""),
        "x-t": headers.get("x-t", ""),
        "x-s-common": headers.get("x-s-common", ""),
        "x-b3-traceid": headers.get("x-b3-traceid", get_trace_id()),
    }
