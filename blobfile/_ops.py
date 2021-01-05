# https://mypy.readthedocs.io/en/stable/common_issues.html#using-classes-that-are-generic-in-stubs-but-not-at-runtime
from __future__ import annotations

import os
import tempfile
import hashlib
import base64
import io
import urllib.parse
import time
import json
import binascii
import stat as stat_module
import glob as local_glob
import re
import shutil
import collections
import itertools
import random
import math
import ssl
import concurrent.futures
import multiprocessing as mp
from typing import (
    overload,
    Optional,
    Tuple,
    Callable,
    Sequence,
    Iterator,
    Mapping,
    Any,
    Dict,
    TextIO,
    BinaryIO,
    cast,
    NamedTuple,
    List,
    Union,
    TYPE_CHECKING,
)

if TYPE_CHECKING:
    # only supported in python 3.8+
    # this works because we postponed evaluation of type annotations with PEP 563,
    # and because we don't use Literal as a base class or for casting
    from typing import Literal


import urllib3
import xmltodict
import filelock

from blobfile import _gcp as gcp, _azure as azure, _common as common
from blobfile._common import (
    Request,
    FileBody,
    Error,
    RequestFailure,
    RestartableStreamingWriteFailure,
    ConcurrentWriteFailure,
    Stat,
    DirEntry,
    Context,
    INVALID_HOSTNAME_STATUS,
)

# feature flags
USE_STREAMING_READ_REQUEST = True

CHUNK_SIZE = 2 ** 20
# max 100MB https://docs.microsoft.com/en-us/rest/api/storageservices/put-block#remarks
# there is a preview version of the API that allows this to be 4000MiB
AZURE_MAX_BLOCK_SIZE = 100_000_000
AZURE_BLOCK_COUNT_LIMIT = 50_000
PARALLEL_COPY_MINIMUM_PART_SIZE = 32 * 2 ** 20

# https://cloud.google.com/storage/docs/naming
# https://www.w3.org/TR/xml/#charsets
INVALID_CHARS = (
    set().union(range(0x0, 0x9)).union(range(0xB, 0xE)).union(range(0xE, 0x20))
)

AZURE_RESPONSE_HEADER_TO_REQUEST_HEADER = {
    "Cache-Control": "x-ms-blob-cache-control",
    "Content-Type": "x-ms-blob-content-type",
    "Content-MD5": "x-ms-blob-content-md5",
    "Content-Encoding": "x-ms-blob-content-encoding",
    "Content-Language": "x-ms-blob-content-language",
    "Content-Disposition": "x-ms-blob-content-disposition",
}

ESCAPED_COLON = "___COLON___"


_context = Context()


def configure(
    *,
    log_callback: Callable[[str], None] = common.default_log_fn,
    connection_pool_max_size: int = common.DEFAULT_CONNECTION_POOL_MAX_SIZE,
    max_connection_pool_count: int = common.DEFAULT_MAX_CONNECTION_POOL_COUNT,
    # https://docs.microsoft.com/en-us/rest/api/storageservices/understanding-block-blobs--append-blobs--and-page-blobs#about-block-blobs
    # the chunk size determines the maximum size of an individual blob
    azure_write_chunk_size: int = common.DEFAULT_AZURE_WRITE_CHUNK_SIZE,
    google_write_chunk_size: int = common.DEFAULT_GOOGLE_WRITE_CHUNK_SIZE,
    retry_log_threshold: int = common.DEFAULT_RETRY_LOG_THRESHOLD,
    retry_limit: Optional[int] = None,
    connect_timeout: Optional[int] = common.DEFAULT_CONNECT_TIMEOUT,
    read_timeout: Optional[int] = common.DEFAULT_READ_TIMEOUT,
    output_az_paths: bool = False,
    use_azure_storage_account_key_fallback: bool = True,
) -> None:
    """
    log_callback: a log callback function `log(msg: string)` to use instead of printing to stdout
    connection_pool_max_size: the max size for each per-host connection pool
    max_connection_pool_count: the maximum count of per-host connection pools
    azure_write_chunk_size: the size of blocks to write to Azure Storage blobs, can be set to a maximum of 100MB
    google_write_chunk_size: the size of blocks to write to Google Cloud Storage blobs in bytes, this only determines the unit of request retries
    retry_log_threshold: set a retry count threshold above which to log failures to the log callback function
    connect_timeout: the maximum amount of time (in seconds) to wait for a connection attempt to a server to succeed, set to None to wait forever
    read_timeout: the maximum amount of time (in seconds) to wait between consecutive read operations for a response from the server, set to None to wait forever
    output_az_paths: output `az://` paths instead of using the `https://` for azure
    use_azure_storage_account_key_fallback: fallback to storage account keys for azure containers, having this enabled (the default) requires listing your subscriptions and may run into 429 errors if you hit the low azure quotas for subscription listing
    """
    global _context
    _context = Context(
        log_callback=log_callback,
        connection_pool_max_size=connection_pool_max_size,
        max_connection_pool_count=max_connection_pool_count,
        azure_write_chunk_size=azure_write_chunk_size,
        retry_log_threshold=retry_log_threshold,
        retry_limit=retry_limit,
        google_write_chunk_size=google_write_chunk_size,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        output_az_paths=output_az_paths,
        use_azure_storage_account_key_fallback=use_azure_storage_account_key_fallback,
    )


def _is_local_path(path: str) -> bool:
    return not _is_gcp_path(path) and not _is_azure_path(path)


def _is_gcp_path(path: str) -> bool:
    url = urllib.parse.urlparse(path)
    return url.scheme == "gs"


def _is_azure_path(path: str) -> bool:
    url = urllib.parse.urlparse(path)
    return (
        url.scheme == "https" and url.netloc.endswith(".blob.core.windows.net")
    ) or url.scheme == "az"


def _download_chunk(src: str, dst: str, start: int, size: int) -> None:
    # this is a little inefficient because each time we open a file we do
    # a query for file metadata, we could call the StreamingReadFile subclass
    # directly with the known size and avoid this
    #
    # in addition, we could provide a fake size (start + size) and change the call
    # to _request_chunk to always specify the end of the file
    # this should cause the connection to be put back into the pool by urllib3
    with BlobFile(src, "rb") as src_f:
        src_f.seek(start)
        # open output file such that we can write directly to the correct range
        with open(dst, "rb+") as dst_f:
            dst_f.seek(start)
            bytes_read = 0
            while True:
                n = min(CHUNK_SIZE, size - bytes_read)
                assert n >= 0
                block = src_f.read(n)
                if block == b"":
                    if bytes_read != size:
                        raise Error(
                            f"read wrong number of bytes from file `{src}`, expected {size} but read {bytes_read}"
                        )
                    break
                dst_f.write(block)
                bytes_read += len(block)


def _parallel_download(
    executor: concurrent.futures.Executor, src: str, dst: str, return_md5: bool
) -> Optional[str]:
    s = stat(src)

    # pre-allocate output file
    with open(dst, "wb") as f:
        f.seek(s.size - 1)
        f.write(b"\0")

    max_workers = getattr(executor, "_max_workers", os.cpu_count() or 1)
    part_size = max(math.ceil(s.size / max_workers), PARALLEL_COPY_MINIMUM_PART_SIZE)
    start = 0
    futures = []
    while start < s.size:
        future = executor.submit(
            _download_chunk, src, dst, start, min(part_size, s.size - start)
        )
        futures.append(future)
        start += part_size
    for future in futures:
        future.result()

    if return_md5:
        with BlobFile(dst, "rb") as f:
            return binascii.hexlify(_block_md5(f)).decode("utf8")


def _azure_upload_chunk(
    path: str, start: int, size: int, url: str, block_id: str
) -> None:
    req = Request(
        url=url,
        method="PUT",
        params=dict(comp="block", blockid=block_id),
        # this needs to be specified since we use a file object for the data
        headers={"Content-Length": str(size)},
        data=FileBody(path, start=start, end=start + size),
        success_codes=(201,),
    )
    azure.execute_api_request(_context, req)


def _azure_finalize_blob(
    path: str, url: str, block_ids: List[str], md5_digest: bytes
) -> None:
    body = {"BlockList": {"Latest": block_ids}}
    req = Request(
        url=url,
        method="PUT",
        # azure does not calculate md5s for us, we have to do that manually
        # https://blogs.msdn.microsoft.com/windowsazurestorage/2011/02/17/windows-azure-blob-md5-overview/
        headers={"x-ms-blob-content-md5": base64.b64encode(md5_digest).decode("utf8")},
        params=dict(comp="blocklist"),
        data=body,
        success_codes=(201, 400),
    )
    resp = azure.execute_api_request(_context, req)
    if resp.status == 400:
        result = xmltodict.parse(resp.data)
        if result["Error"]["Code"] == "InvalidBlockList":
            # the most likely way this could happen is if the file was deleted while
            # we were uploading, so assume that is what happened
            # this could be interpreted as a sort of RestartableStreamingWriteFailure but
            # that could result in two processes fighting while uploading the file
            raise ConcurrentWriteFailure.create_from_request_response(
                f"Invalid block list, most likely a concurrent writer wrote to the same path: `{path}`",
                request=req,
                response=resp,
            )
        else:
            raise RequestFailure.create_from_request_response(
                message=f"unexpected status {resp.status}", request=req, response=resp
            )


def _azure_block_index_to_block_id(index: int, upload_id: int) -> str:
    assert index < 2 ** 17
    id_plus_index = (upload_id << 17) + index
    assert id_plus_index < 2 ** 64
    return base64.b64encode(id_plus_index.to_bytes(8, byteorder="big")).decode("utf8")


def _azure_parallel_upload(
    executor: concurrent.futures.Executor, src: str, dst: str, return_md5: bool
) -> Optional[str]:
    assert _is_local_path(src) and _is_azure_path(dst)

    with BlobFile(src, "rb") as f:
        md5_digest = _block_md5(f)

    account, container, blob = azure.split_path(dst)
    dst_url = azure.build_url(
        account, "/{container}/{blob}", container=container, blob=blob
    )

    upload_id = random.randint(0, 2 ** 47 - 1)
    s = stat(src)
    block_ids = []
    max_workers = getattr(executor, "_max_workers", os.cpu_count() or 1)
    part_size = min(
        max(math.ceil(s.size / max_workers), PARALLEL_COPY_MINIMUM_PART_SIZE),
        AZURE_MAX_BLOCK_SIZE,
    )
    i = 0
    start = 0
    futures = []
    while start < s.size:
        block_id = _azure_block_index_to_block_id(i, upload_id)
        future = executor.submit(
            _azure_upload_chunk,
            src,
            start,
            min(_context.azure_write_chunk_size, s.size - start),
            dst_url,
            block_id,
        )
        futures.append(future)
        block_ids.append(block_id)
        i += 1
        start += part_size
    for future in futures:
        future.result()

    _azure_finalize_blob(
        path=dst, url=dst_url, block_ids=block_ids, md5_digest=md5_digest
    )
    return binascii.hexlify(md5_digest).decode("utf8") if return_md5 else None


def _gcp_upload_part(path: str, start: int, size: int, dst: str) -> str:
    bucket, blob = gcp.split_path(dst)
    req = Request(
        url=gcp.build_url("/upload/storage/v1/b/{bucket}/o", bucket=bucket),
        method="POST",
        params=dict(uploadType="media", name=blob),
        data=FileBody(path, start=start, end=start + size),
        success_codes=(200,),
    )
    resp = gcp.execute_api_request(_context, req)
    metadata = json.loads(resp.data)
    return metadata["generation"]


def _gcp_delete_part(bucket: str, name: str) -> None:
    req = Request(
        url=gcp.build_url(
            "/storage/v1/b/{bucket}/o/{object}", bucket=bucket, object=name
        ),
        method="DELETE",
        success_codes=(204, 404),
    )
    gcp.execute_api_request(_context, req)


def _gcp_parallel_upload(
    executor: concurrent.futures.Executor, src: str, dst: str, return_md5: bool
) -> Optional[str]:
    assert _is_local_path(src) and _is_gcp_path(dst)

    with BlobFile(src, "rb") as f:
        md5_digest = _block_md5(f)

    s = stat(src)

    dstbucket, dstname = gcp.split_path(dst)
    source_objects = []
    object_names = []
    max_workers = getattr(executor, "_max_workers", os.cpu_count() or 1)
    part_size = max(math.ceil(s.size / max_workers), PARALLEL_COPY_MINIMUM_PART_SIZE)
    i = 0
    start = 0
    futures = []
    while start < s.size:
        suffix = f".part.{i}"
        future = executor.submit(
            _gcp_upload_part, src, start, min(part_size, s.size - start), dst + suffix
        )
        futures.append(future)
        object_names.append(dstname + suffix)
        i += 1
        start += part_size
    for name, future in zip(object_names, futures):
        generation = future.result()
        source_objects.append(
            {
                "name": name,
                "generation": generation,
                "objectPreconditions": {"ifGenerationMatch": generation},
            }
        )

    req = Request(
        url=gcp.build_url(
            "/storage/v1/b/{destinationBucket}/o/{destinationObject}/compose",
            destinationBucket=dstbucket,
            destinationObject=dstname,
        ),
        method="POST",
        data={"sourceObjects": source_objects},
        success_codes=(200,),
    )
    resp = gcp.execute_api_request(_context, req)
    metadata = json.loads(resp.data)
    hexdigest = binascii.hexlify(md5_digest).decode("utf8")
    _gcp_maybe_update_md5(dst, metadata["generation"], hexdigest)

    # delete parts in parallel
    delete_futures = []
    for name in object_names:
        future = executor.submit(_gcp_delete_part, dstbucket, name)
        delete_futures.append(future)
    for future in delete_futures:
        future.result()

    return hexdigest if return_md5 else None


def copy(
    src: str,
    dst: str,
    overwrite: bool = False,
    parallel: bool = False,
    parallel_executor: Optional[concurrent.futures.Executor] = None,
    return_md5: bool = False,
) -> Optional[str]:
    """
    Copy a file from one path to another

    If both paths are on the same blob storage, this will perform a remote copy operation without downloading
    the contents locally.

    If `overwrite` is `False` (the default), an exception will be raised if the destination
    path exists.

    If `parallel` is `True`, use multiple processes to dowload or upload the file.  For this to work, one path must be on blob storage and the other path must be local.  This can be faster on cloud machines but is not in general guaranteed to be faster than using serial copy.  The default is `False`.

    If `parallel_executor` is set to a `concurrent.futures.Executor` and `parallel` is set to `True`, the provided executor will be used instead of creating a new one for each call to `copy()`.

    If `return_md5` is set to `True`, an md5 will be calculated during the copy and returned if available,
    or else None will be returned.
    """
    # it would be best to check isdir() for remote paths, but that would
    # involve 2 extra network requests, so just do this test instead
    if _guess_isdir(src):
        raise IsADirectoryError(f"Is a directory: '{src}'")
    if _guess_isdir(dst):
        raise IsADirectoryError(f"Is a directory: '{dst}'")

    if not overwrite:
        if exists(dst):
            raise FileExistsError(
                f"Destination '{dst}' already exists and overwrite is disabled"
            )

    # special case cloud to cloud copy, don't download the file
    if _is_gcp_path(src) and _is_gcp_path(dst):
        srcbucket, srcname = gcp.split_path(src)
        dstbucket, dstname = gcp.split_path(dst)
        params = {}
        while True:
            req = Request(
                url=gcp.build_url(
                    "/storage/v1/b/{sourceBucket}/o/{sourceObject}/rewriteTo/b/{destinationBucket}/o/{destinationObject}",
                    sourceBucket=srcbucket,
                    sourceObject=srcname,
                    destinationBucket=dstbucket,
                    destinationObject=dstname,
                ),
                method="POST",
                params=params,
                success_codes=(200, 404),
            )
            resp = gcp.execute_api_request(_context, req)
            if resp.status == 404:
                raise FileNotFoundError(f"Source file not found: '{src}'")
            result = json.loads(resp.data)
            if result["done"]:
                if return_md5:
                    return gcp.get_md5(result["resource"])
                else:
                    return
            params["rewriteToken"] = result["rewriteToken"]

    if _is_azure_path(src) and _is_azure_path(dst):
        # https://docs.microsoft.com/en-us/rest/api/storageservices/copy-blob
        dst_account, dst_container, dst_blob = azure.split_path(dst)
        src_account, src_container, src_blob = azure.split_path(src)

        def build_req() -> Request:
            src_url = azure.build_url(
                src_account,
                "/{container}/{blob}",
                container=src_container,
                blob=src_blob,
            )
            if src_account != dst_account:
                # the signed url can expire, so technically we should get the sas_token and build the signed url
                # each time we build a new request
                sas_token = azure.sas_token_manager.get_token(
                    ctx=_context, key=(src_account, src_container)
                )
                # if we don't get a token, it's likely we have anonymous access to the container
                # if we do get a token, the container is likely private and we need to use
                # a signed url as the source
                if sas_token is not None:
                    src_url, _ = azure.generate_signed_url(key=sas_token, url=src_url)
            req = Request(
                url=azure.build_url(
                    dst_account,
                    "/{container}/{blob}",
                    container=dst_container,
                    blob=dst_blob,
                ),
                method="PUT",
                headers={"x-ms-copy-source": src_url},
                success_codes=(202, 404),
            )
            return azure.create_api_request(
                req,
                auth=azure.access_token_manager.get_token(
                    ctx=_context, key=(dst_account, dst_container)
                ),
            )

        resp = common.execute_request(_context, build_req)
        if resp.status == 404:
            raise FileNotFoundError(f"Source file not found: '{src}'")
        copy_id = resp.headers["x-ms-copy-id"]
        copy_status = resp.headers["x-ms-copy-status"]
        etag = resp.headers["etag"]

        # wait for potentially async copy operation to finish
        # https://docs.microsoft.com/en-us/rest/api/storageservices/get-blob
        # pending, success, aborted, failed
        backoff = common.exponential_sleep_generator()
        while copy_status == "pending":
            time.sleep(next(backoff))
            req = Request(
                url=azure.build_url(
                    dst_account,
                    "/{container}/{blob}",
                    container=dst_container,
                    blob=dst_blob,
                ),
                method="GET",
            )
            resp = azure.execute_api_request(_context, req)
            if resp.headers["x-ms-copy-id"] != copy_id:
                raise Error("Copy id mismatch")
            etag = resp.headers["etag"]
            copy_status = resp.headers["x-ms-copy-status"]
        if copy_status != "success":
            raise Error(f"Invalid copy status: '{copy_status}'")
        if return_md5:
            # if the file is the same one that we just copied, return the stored MD5
            st = _azure_maybe_stat(dst)
            if st is not None and st.version == etag:
                return st.md5
        return

    if parallel:
        copy_fn = None
        if (_is_azure_path(src) or _is_gcp_path(src)) and _is_local_path(dst):
            copy_fn = _parallel_download

        if _is_local_path(src) and _is_azure_path(dst):
            copy_fn = _azure_parallel_upload

        if _is_local_path(src) and _is_gcp_path(dst):
            copy_fn = _gcp_parallel_upload

        if copy_fn is not None:
            if parallel_executor is None:
                with concurrent.futures.ProcessPoolExecutor() as executor:
                    return copy_fn(executor, src, dst, return_md5=return_md5)
            else:
                return copy_fn(parallel_executor, src, dst, return_md5=return_md5)

    for attempt, backoff in enumerate(common.exponential_sleep_generator()):
        try:
            with BlobFile(src, "rb", streaming=True) as src_f, BlobFile(
                dst, "wb", streaming=True
            ) as dst_f:
                m = hashlib.md5()
                while True:
                    block = src_f.read(CHUNK_SIZE)
                    if block == b"":
                        break
                    if return_md5:
                        m.update(block)
                    dst_f.write(block)
                if return_md5:
                    return m.hexdigest()
                else:
                    return
        except RestartableStreamingWriteFailure as err:
            # currently this is the only type of failure we retry, since we can re-read the source
            # stream from the beginning
            # if this failure occurs, the upload must be restarted from the beginning
            # https://cloud.google.com/storage/docs/resumable-uploads#practices
            # https://github.com/googleapis/gcs-resumable-upload/issues/15#issuecomment-249324122
            if _context.retry_limit is not None and attempt >= _context.retry_limit:
                raise

            if attempt >= _context.retry_log_threshold:
                _context.log_callback(
                    f"error {err} when executing a streaming write to {dst} attempt {attempt}, sleeping for {backoff:.1f} seconds before retrying"
                )
            time.sleep(backoff)


def _calc_range(start: Optional[int] = None, end: Optional[int] = None) -> str:
    # https://cloud.google.com/storage/docs/xml-api/get-object-download
    # oddly range requests are not mentioned in the JSON API, only in the XML api
    if start is not None and end is not None:
        return f"bytes={start}-{end-1}"
    elif start is not None:
        return f"bytes={start}-"
    elif end is not None:
        if end > 0:
            return f"bytes=0-{end-1}"
        else:
            return f"bytes=-{-int(end)}"
    else:
        raise Error("Invalid range")


def _create_gcp_page_iterator(
    url: str, method: str, params: Mapping[str, str]
) -> Iterator[Dict[str, Any]]:
    p = dict(params).copy()

    while True:
        req = Request(url=url, method=method, params=p, success_codes=(200, 404))
        resp = gcp.execute_api_request(_context, req)
        if resp.status == 404:
            return
        result = json.loads(resp.data)
        yield result
        if "nextPageToken" not in result:
            break
        p["pageToken"] = result["nextPageToken"]


def _create_azure_page_iterator(
    url: str,
    method: str,
    data: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, str]] = None,
) -> Iterator[Dict[str, Any]]:
    if params is None:
        p = {}
    else:
        p = dict(params).copy()
    if data is None:
        d = None
    else:
        d = dict(data).copy()
    while True:
        req = Request(
            url=url,
            method=method,
            params=p,
            data=d,
            success_codes=(200, 404, INVALID_HOSTNAME_STATUS),
        )
        resp = azure.execute_api_request(_context, req)
        if resp.status in (404, INVALID_HOSTNAME_STATUS):
            return
        result = xmltodict.parse(resp.data)["EnumerationResults"]
        yield result
        if result["NextMarker"] is None:
            break
        p["marker"] = result["NextMarker"]


def _gcp_get_entries(bucket: str, result: Mapping[str, Any]) -> Iterator[DirEntry]:
    if "prefixes" in result:
        for p in result["prefixes"]:
            path = gcp.combine_path(bucket, p)
            yield _entry_from_dirpath(path)
    if "items" in result:
        for item in result["items"]:
            path = gcp.combine_path(bucket, item["name"])
            if item["name"].endswith("/"):
                yield _entry_from_dirpath(path)
            else:
                yield _entry_from_path_stat(path, gcp.make_stat(item))


def _azure_get_entries(
    account: str, container: str, result: Mapping[str, Any]
) -> Iterator[DirEntry]:
    blobs = result["Blobs"]
    if blobs is None:
        return
    if "BlobPrefix" in blobs:
        if isinstance(blobs["BlobPrefix"], dict):
            blobs["BlobPrefix"] = [blobs["BlobPrefix"]]
        for bp in blobs["BlobPrefix"]:
            path = _azure_combine_path(account, container, bp["Name"])
            yield _entry_from_dirpath(path)
    if "Blob" in blobs:
        if isinstance(blobs["Blob"], dict):
            blobs["Blob"] = [blobs["Blob"]]
        for b in blobs["Blob"]:
            path = _azure_combine_path(account, container, b["Name"])
            if b["Name"].endswith("/"):
                yield _entry_from_dirpath(path)
            else:
                props = b["Properties"]
                yield _entry_from_path_stat(path, azure.make_stat(props))


def _gcp_maybe_stat(path: str) -> Optional[Stat]:
    bucket, blob = gcp.split_path(path)
    if blob == "":
        return None
    req = Request(
        url=gcp.build_url(
            "/storage/v1/b/{bucket}/o/{object}", bucket=bucket, object=blob
        ),
        method="GET",
        success_codes=(200, 404),
    )
    resp = gcp.execute_api_request(_context, req)
    if resp.status != 200:
        return None
    return gcp.make_stat(json.loads(resp.data))


def _azure_maybe_stat(path: str) -> Optional[Stat]:
    account, container, blob = azure.split_path(path)
    if blob == "":
        return None
    req = Request(
        url=azure.build_url(
            account, "/{container}/{blob}", container=container, blob=blob
        ),
        method="HEAD",
        success_codes=(200, 404, INVALID_HOSTNAME_STATUS),
    )
    resp = azure.execute_api_request(_context, req)
    if resp.status != 200:
        return None
    return azure.make_stat(resp.headers)


def exists(path: str) -> bool:
    """
    Return true if that path exists (either as a file or a directory)
    """
    if _is_local_path(path):
        return os.path.exists(path)
    elif _is_gcp_path(path):
        st = _gcp_maybe_stat(path)
        if st is not None:
            return True
        return isdir(path)
    elif _is_azure_path(path):
        st = _azure_maybe_stat(path)
        if st is not None:
            return True
        return isdir(path)
    else:
        raise Error(f"Unrecognized path: '{path}'")


def basename(path: str) -> str:
    """
    Get the filename component of the path

    For GCS, this is the part after the bucket
    """
    if _is_gcp_path(path):
        _, obj = gcp.split_path(path)
        return obj.split("/")[-1]
    elif _is_azure_path(path):
        _, _, obj = azure.split_path(path)
        return obj.split("/")[-1]
    else:
        return os.path.basename(path)


def _string_overlap(s1: str, s2: str) -> int:
    length = min(len(s1), len(s2))
    for i in range(length):
        if s1[i] != s2[i]:
            return i
    return length


def _split_path(path: str) -> List[str]:
    # a/b/c => a/, b/, c
    # a/b/ => a/, b/
    # /a/b/c => /, a/, b/, c
    parts = []
    part = ""
    for c in path:
        part += c
        if c == "/":
            parts.append(part)
            part = ""
    if part != "":
        parts.append(part)
    return parts


def _entry_from_dirpath(path: str) -> DirEntry:
    path = _strip_slash(path)
    return DirEntry(
        name=basename(path), path=path, is_dir=True, is_file=False, stat=None
    )


def _entry_from_path_stat(path: str, stat: Stat) -> DirEntry:
    assert not path.endswith("/")
    return DirEntry(
        name=basename(path), path=path, is_dir=False, is_file=True, stat=stat
    )


def _expand_implicit_dirs(root: str, it: Iterator[DirEntry]) -> Iterator[DirEntry]:
    # blob storage does not always have definitions for each intermediate dir
    # if we have a listing like
    #  gs://test/a/b
    #  gs://test/a/b/c/d
    # then we emit an entry "gs://test/a/b/c" for the implicit dir "c"
    # requires that iterator return objects in sorted order
    previous_path = root
    for entry in it:
        # find the overlap between the previous_path and the current
        entry_slash_path = _get_slash_path(entry)
        offset = _string_overlap(previous_path, entry_slash_path)
        relpath = entry_slash_path[offset:]
        cur = entry_slash_path[:offset]
        if len(relpath) == 0:
            yield _entry_from_dirpath(cur)
        else:
            for part in _split_path(relpath):
                cur += part
                yield _entry_from_dirpath(cur)
        assert entry_slash_path >= previous_path
        previous_path = entry_slash_path


def _compile_pattern(s: str):
    tokens = [t for t in re.split("([*]+)", s) if t != ""]
    regexp = ""
    for tok in tokens:
        if tok == "*":
            regexp += r"[^/]*"
        elif tok == "**":
            regexp += r".*"
        else:
            regexp += re.escape(tok)
    return re.compile(regexp + r"/?$")


def _glob_full(pattern: str) -> Iterator[DirEntry]:
    prefix, _, _ = pattern.partition("*")

    re_pattern = _compile_pattern(pattern)

    for entry in _expand_implicit_dirs(root=prefix, it=_list_blobs(path=prefix)):
        entry_slash_path = _get_slash_path(entry)
        if bool(re_pattern.match(entry_slash_path)):
            if entry_slash_path == prefix and entry.is_dir:
                # we matched the parent directory
                continue
            yield entry


class _GlobTask(NamedTuple):
    cur: str
    rem: Sequence[str]


class _GlobEntry(NamedTuple):
    entry: DirEntry


class _GlobTaskComplete(NamedTuple):
    pass


def _process_glob_task(
    root: str, t: _GlobTask
) -> Iterator[Union[_GlobTask, _GlobEntry]]:
    cur = t.cur + t.rem[0]
    rem = t.rem[1:]
    if "**" in cur:
        for entry in _glob_full(root + cur + "".join(rem)):
            yield _GlobEntry(entry)
    elif "*" in cur:
        re_pattern = _compile_pattern(root + cur)
        prefix, _, _ = cur.partition("*")
        path = root + prefix
        for entry in _list_blobs(path=path, delimiter="/"):
            entry_slash_path = _get_slash_path(entry)
            # in the case of dirname/* we should not return the path dirname/
            if entry_slash_path == path and entry.is_dir:
                # we matched the parent directory
                continue
            if bool(re_pattern.match(entry_slash_path)):
                if len(rem) == 0:
                    yield _GlobEntry(entry)
                else:
                    assert entry_slash_path.startswith(root)
                    yield _GlobTask(entry_slash_path[len(root) :], rem)
    else:
        if len(rem) == 0:
            path = root + cur
            entry = _get_entry(path)
            if entry is not None:
                yield _GlobEntry(entry)
        else:
            yield _GlobTask(cur, rem)


def _glob_worker(
    root: str,
    tasks: mp.Queue[_GlobTask],
    results: mp.Queue[Union[_GlobEntry, _GlobTask, _GlobTaskComplete]],
) -> None:
    while True:
        t = tasks.get()
        for r in _process_glob_task(root=root, t=t):
            results.put(r)
        results.put(_GlobTaskComplete())


def _local_glob(pattern: str) -> Iterator[str]:
    for filepath in local_glob.iglob(pattern, recursive=True):
        filepath = os.path.normpath(filepath)
        if filepath.endswith(os.sep):
            filepath = filepath[:-1]
        yield filepath


def glob(pattern: str, parallel: bool = False) -> Iterator[str]:
    """
    Find files and directories matching a pattern. Supports * and **

    For local paths, this function uses glob.glob() which has special handling for * and **
    that is not quite the same as remote paths.  See https://cloud.google.com/storage/docs/gsutil/addlhelp/WildcardNames#different-behavior-for-dot-files-in-local-file-system_1 for more information.

    Globs can have confusing performance, see https://cloud.google.com/storage/docs/gsutil/addlhelp/WildcardNames#efficiency-consideration:-using-wildcards-over-many-objects for more information.

    You can set `parallel=True` to use multiple processes to perform the glob.  It's likely
    that the results will no longer be in order.
    """
    if _is_local_path(pattern):
        # scanglob currently does an os.stat for each matched file
        # until scanglob can be implemented directly on scandir
        # this code is here to not
        if "?" in pattern or "[" in pattern or "]" in pattern:
            raise Error("Advanced glob queries are not supported")
        yield from _local_glob(pattern)
    else:
        for entry in scanglob(pattern=pattern, parallel=parallel):
            yield entry.path


def scanglob(pattern: str, parallel: bool = False) -> Iterator[DirEntry]:
    """
    Same as `glob`, but returns `DirEntry` objects instead of strings
    """
    if "?" in pattern or "[" in pattern or "]" in pattern:
        raise Error("Advanced glob queries are not supported")

    if _is_local_path(pattern):
        for filepath in _local_glob(pattern):
            # doing a stat call for each file isn't the most efficient
            # iglob uses os.scandir internally, but doesn't expose the information from that, so we'd
            # need to re-implement local glob
            # we could make the behavior with remote glob more consistent though if we did that
            s = os.stat(filepath)
            is_dir = stat_module.S_ISDIR(s.st_mode)
            yield DirEntry(
                path=filepath,
                name=basename(filepath),
                is_dir=is_dir,
                is_file=not is_dir,
                stat=None
                if is_dir
                else Stat(
                    size=s.st_size,
                    mtime=s.st_mtime,
                    ctime=s.st_ctime,
                    md5=None,
                    version=None,
                ),
            )
    elif _is_gcp_path(pattern) or _is_azure_path(pattern):
        if "*" not in pattern:
            entry = _get_entry(pattern)
            if entry is not None:
                yield entry
            return

        if _is_gcp_path(pattern):
            bucket, blob_prefix = gcp.split_path(pattern)
            if "*" in bucket:
                raise Error("Wildcards cannot be used in bucket name")
            root = gcp.combine_path(bucket, "")
        else:
            account, container, blob_prefix = azure.split_path(pattern)
            if "*" in account or "*" in container:
                raise Error("Wildcards cannot be used in account or container")
            root = _azure_combine_path(account, container, "")

        initial_task = _GlobTask("", _split_path(blob_prefix))

        if parallel:
            tasks = mp.Queue()
            tasks.put(initial_task)
            tasks_enqueued = 1
            results = mp.Queue()

            tasks_done = 0
            with mp.Pool(initializer=_glob_worker, initargs=(root, tasks, results)):
                while tasks_done < tasks_enqueued:
                    r = results.get()
                    if isinstance(r, _GlobEntry):
                        yield r.entry
                    elif isinstance(r, _GlobTask):
                        tasks.put(r)
                        tasks_enqueued += 1
                    elif isinstance(r, _GlobTaskComplete):
                        tasks_done += 1
                    else:
                        raise Error("Invalid result")
        else:
            dq: collections.deque[_GlobTask] = collections.deque()
            dq.append(initial_task)
            while len(dq) > 0:
                t = dq.popleft()
                for r in _process_glob_task(root=root, t=t):
                    if isinstance(r, _GlobEntry):
                        yield r.entry
                    else:
                        dq.append(r)
    else:
        raise Error(f"Unrecognized path '{pattern}'")


def _strip_slash(path: str) -> str:
    if path.endswith("/"):
        return path[:-1]
    else:
        return path


def _strip_slashes(path: str) -> str:
    while path.endswith("/"):
        path = path[:-1]
    return path


def isdir(path: str) -> bool:
    """
    Return true if a path is an existing directory
    """
    if _is_local_path(path):
        return os.path.isdir(path)
    elif _is_gcp_path(path):
        if not path.endswith("/"):
            path += "/"
        bucket, blob = gcp.split_path(path)
        if blob == "":
            req = Request(
                url=gcp.build_url("/storage/v1/b/{bucket}", bucket=bucket),
                method="GET",
                success_codes=(200, 404),
            )
            resp = gcp.execute_api_request(_context, req)
            return resp.status == 200
        else:
            params = dict(prefix=blob, delimiter="/", maxResults="1")
            req = Request(
                url=gcp.build_url("/storage/v1/b/{bucket}/o", bucket=bucket),
                method="GET",
                params=params,
                success_codes=(200, 404),
            )
            resp = gcp.execute_api_request(_context, req)
            if resp.status == 404:
                return False
            result = json.loads(resp.data)
            return "items" in result or "prefixes" in result
    elif _is_azure_path(path):
        if not path.endswith("/"):
            path += "/"
        account, container, blob = azure.split_path(path)
        if blob == "":
            req = Request(
                url=azure.build_url(
                    account, "/{container}", container=container, blob=blob
                ),
                method="GET",
                params=dict(restype="container"),
                success_codes=(200, 404, INVALID_HOSTNAME_STATUS),
            )
            resp = azure.execute_api_request(_context, req)
            return resp.status == 200
        else:
            # even though we're only interested in having one result, we still need to make an
            # iterator. as it happens, azure is perfectly willing to return an empty first page.
            it = _create_azure_page_iterator(
                url=azure.build_url(account, "/{container}", container=container),
                method="GET",
                params=dict(
                    comp="list",
                    restype="container",
                    prefix=blob,
                    delimiter="/",
                    maxresults="1",
                ),
            )
            for result in it:
                if result["Blobs"] is not None:
                    return "BlobPrefix" in result["Blobs"] or "Blob" in result["Blobs"]
            return False
    else:
        raise Error(f"Unrecognized path: '{path}'")


def _guess_isdir(path: str) -> bool:
    """
    Guess if a path is a directory without performing network requests
    """
    if _is_local_path(path) and os.path.isdir(path):
        return True
    elif (_is_gcp_path(path) or _is_azure_path(path)) and path.endswith("/"):
        return True
    return False


def _gcp_list_blobs(path: str, delimiter: Optional[str] = None) -> Iterator[DirEntry]:
    params = {}
    if delimiter is not None:
        params["delimiter"] = delimiter

    bucket, prefix = gcp.split_path(path)
    it = _create_gcp_page_iterator(
        url=gcp.build_url("/storage/v1/b/{bucket}/o", bucket=bucket),
        method="GET",
        params=dict(prefix=prefix, **params),
    )
    for result in it:
        for entry in _gcp_get_entries(bucket, result):
            yield entry


def _azure_list_blobs(path: str, delimiter: Optional[str] = None) -> Iterator[DirEntry]:
    params = {}
    if delimiter is not None:
        params["delimiter"] = delimiter

    account, container, prefix = azure.split_path(path)
    it = _create_azure_page_iterator(
        url=azure.build_url(account, "/{container}", container=container),
        method="GET",
        params=dict(comp="list", restype="container", prefix=prefix, **params),
    )
    for result in it:
        for entry in _azure_get_entries(account, container, result):
            yield entry


def _list_blobs(path: str, delimiter: Optional[str] = None) -> Iterator[DirEntry]:
    params = {}
    if delimiter is not None:
        params["delimiter"] = delimiter

    if _is_gcp_path(path):
        yield from _gcp_list_blobs(path, delimiter=delimiter)
    elif _is_azure_path(path):
        yield from _azure_list_blobs(path, delimiter=delimiter)
    else:
        raise Error(f"Unrecognized path: '{path}'")


def _get_slash_path(entry: DirEntry) -> str:
    return entry.path + "/" if entry.is_dir else entry.path


def _azure_combine_path(account: str, container: str, obj: str) -> str:
    if _context.output_az_paths:
        return azure.combine_az_path(account, container, obj)
    else:
        return azure.combine_https_path(account, container, obj)


def _normalize_path(path: str) -> str:
    # convert paths to the canonical format
    if _is_azure_path(path):
        return _azure_combine_path(*azure.split_path(path))
    return path


def _list_blobs_in_dir(prefix: str, exclude_prefix: bool) -> Iterator[DirEntry]:
    # the prefix check doesn't work without normalization
    normalized_prefix = _normalize_path(prefix)
    for entry in _list_blobs(path=normalized_prefix, delimiter="/"):
        if exclude_prefix and _get_slash_path(entry) == normalized_prefix:
            continue
        yield entry


def listdir(path: str, shard_prefix_length: int = 0) -> Iterator[str]:
    """
    Returns an iterator of the contents of the directory at `path`

    If your filenames are uniformly distributed (like hashes) then you can use `shard_prefix_length`
    to query them more quickly.  `shard_prefix_length` will do multiple queries in parallel,
    querying each possible prefix independently.

    Using `shard_prefix_length` will only consider prefixes that are not unusual characters
    (mostly these are ascii values < 0x20) some of these could technically show up in a path.
    """
    for entry in scandir(path, shard_prefix_length=shard_prefix_length):
        yield entry.name


def scandir(path: str, shard_prefix_length: int = 0) -> Iterator[DirEntry]:
    """
    Same as `listdir`, but returns `DirEntry` objects instead of strings
    """
    if (_is_gcp_path(path) or _is_azure_path(path)) and not path.endswith("/"):
        path += "/"
    if not exists(path):
        raise FileNotFoundError(f"The system cannot find the path specified: '{path}'")
    if not isdir(path):
        raise NotADirectoryError(f"The directory name is invalid: '{path}'")
    if _is_local_path(path):
        for de in os.scandir(path):
            if de.is_dir():
                yield DirEntry(
                    name=de.name,
                    path=os.path.abspath(de.path),
                    is_dir=True,
                    is_file=False,
                    stat=None,
                )
            else:
                s = de.stat()
                yield DirEntry(
                    name=de.name,
                    path=os.path.abspath(de.path),
                    is_dir=False,
                    is_file=True,
                    stat=Stat(
                        size=s.st_size,
                        mtime=s.st_mtime,
                        ctime=s.st_ctime,
                        md5=None,
                        version=None,
                    ),
                )
    elif _is_gcp_path(path) or _is_azure_path(path):
        if shard_prefix_length == 0:
            yield from _list_blobs_in_dir(path, exclude_prefix=True)
        else:
            prefixes = mp.Queue()
            items = mp.Queue()
            tasks_enqueued = 0

            valid_chars = [
                i for i in range(256) if i not in INVALID_CHARS and i != ord("/")
            ]
            for repeat in range(1, shard_prefix_length + 1):
                for chars in itertools.product(valid_chars, repeat=repeat):
                    prefix = ""
                    for c in chars:
                        prefix += chr(c)
                    # we need to check for exact matches for shorter prefix lengths
                    # if we only searched for prefixes of length `shard_prefix_length`
                    # we would skip shorter names, for instance "a" would be skipped if we
                    # we had `shard_prefix_length=2`
                    # instead we check for an exact match for everything shorter than
                    # our `shard_prefix_length`
                    exact = repeat != shard_prefix_length
                    prefixes.put((path, prefix, exact))
                    tasks_enqueued += 1

            tasks_done = 0
            with mp.Pool(
                initializer=_sharded_listdir_worker, initargs=(prefixes, items)
            ):
                while tasks_done < tasks_enqueued:
                    entry = items.get()
                    if entry is None:
                        tasks_done += 1
                        continue
                    yield entry
    else:
        raise Error(f"Unrecognized path: '{path}'")


def _get_entry(path: str) -> Optional[DirEntry]:
    if _is_gcp_path(path):
        st = _gcp_maybe_stat(path)
        if st is not None:
            if path.endswith("/"):
                return _entry_from_dirpath(path)
            else:
                return _entry_from_path_stat(path, st)
    elif _is_azure_path(path):
        st = _azure_maybe_stat(path)
        if st is not None:
            if path.endswith("/"):
                return _entry_from_dirpath(path)
            else:
                return _entry_from_path_stat(path, st)
    else:
        raise Error(f"Unrecognized path: '{path}'")

    if isdir(path):
        return _entry_from_dirpath(path)
    return None


def _sharded_listdir_worker(
    prefixes: mp.Queue[Tuple[str, str, bool]], items: mp.Queue[Optional[DirEntry]]
) -> None:
    while True:
        base, prefix, exact = prefixes.get(True)
        if exact:
            path = base + prefix
            entry = _get_entry(path)
            if entry is not None:
                items.put(entry)
        else:
            it = _list_blobs_in_dir(base + prefix, exclude_prefix=False)
            for entry in it:
                items.put(entry)
        items.put(None)  # indicate that we have finished this path


def makedirs(path: str) -> None:
    """
    Make any directories necessary to ensure that path is a directory
    """
    if _is_local_path(path):
        os.makedirs(path, exist_ok=True)
    elif _is_gcp_path(path):
        if not path.endswith("/"):
            path += "/"
        bucket, blob = gcp.split_path(path)
        req = Request(
            url=gcp.build_url("/upload/storage/v1/b/{bucket}/o", bucket=bucket),
            method="POST",
            params=dict(uploadType="media", name=blob),
            success_codes=(200, 400),
        )
        resp = gcp.execute_api_request(_context, req)
        if resp.status == 400:
            raise Error(f"Unable to create directory, bucket does not exist: '{path}'")
    elif _is_azure_path(path):
        if not path.endswith("/"):
            path += "/"
        account, container, blob = azure.split_path(path)
        req = Request(
            url=azure.build_url(
                account, "/{container}/{blob}", container=container, blob=blob
            ),
            method="PUT",
            headers={"x-ms-blob-type": "BlockBlob"},
            success_codes=(201, 400),
        )
        resp = azure.execute_api_request(_context, req)
        if resp.status == 400:
            raise Error(
                f"Unable to create directory, account/container does not exist: '{path}'"
            )
    else:
        raise Error(f"Unrecognized path: '{path}'")


def remove(path: str) -> None:
    """
    Remove a file at the given path
    """
    if _is_local_path(path):
        os.remove(path)
    elif _is_gcp_path(path):
        if path.endswith("/"):
            raise IsADirectoryError(f"Is a directory: '{path}'")
        bucket, blob = gcp.split_path(path)
        if blob == "":
            raise FileNotFoundError(
                f"The system cannot find the path specified: '{path}'"
            )
        req = Request(
            url=gcp.build_url(
                "/storage/v1/b/{bucket}/o/{object}", bucket=bucket, object=blob
            ),
            method="DELETE",
            success_codes=(204, 404),
        )
        resp = gcp.execute_api_request(_context, req)
        if resp.status == 404:
            raise FileNotFoundError(
                f"The system cannot find the path specified: '{path}'"
            )
    elif _is_azure_path(path):
        if path.endswith("/"):
            raise IsADirectoryError(f"Is a directory: '{path}'")
        account, container, blob = azure.split_path(path)
        if blob == "":
            raise FileNotFoundError(
                f"The system cannot find the path specified: '{path}'"
            )
        req = Request(
            url=azure.build_url(
                account, "/{container}/{blob}", container=container, blob=blob
            ),
            method="DELETE",
            success_codes=(202, 404, INVALID_HOSTNAME_STATUS),
        )
        resp = azure.execute_api_request(_context, req)
        if resp.status in (404, INVALID_HOSTNAME_STATUS):
            raise FileNotFoundError(
                f"The system cannot find the path specified: '{path}'"
            )
    else:
        raise Error(f"Unrecognized path: '{path}'")


def rmdir(path: str) -> None:
    """
    Remove an empty directory at the given path
    """
    if _is_local_path(path):
        os.rmdir(path)
        return

    # directories in blob storage are different from normal directories
    # a directory exists if there are any blobs that have that directory as a prefix
    # when the last blob with that prefix is deleted, the directory no longer exists
    # except in the case when there is a blob with a name ending in a slash
    # representing an empty directory

    # to make this more usable it is not an error to delete a directory that does
    # not exist, but is still an error to delete a non-empty one
    if not path.endswith("/"):
        path += "/"

    if _is_gcp_path(path):
        _, blob = gcp.split_path(path)
    elif _is_azure_path(path):
        _, _, blob = azure.split_path(path)
    else:
        raise Error(f"Unrecognized path: '{path}'")

    if blob == "":
        raise Error(f"Cannot delete bucket: '{path}'")
    it = listdir(path)
    try:
        next(it)
    except FileNotFoundError:
        # this directory does not exist
        return
    except StopIteration:
        # this directory exists and is empty
        pass
    else:
        # this directory exists but is not empty
        raise OSError(f"The directory is not empty: '{path}'")

    if _is_gcp_path(path):
        bucket, blob = gcp.split_path(path)
        req = Request(
            url=gcp.build_url(
                "/storage/v1/b/{bucket}/o/{object}", bucket=bucket, object=blob
            ),
            method="DELETE",
            success_codes=(204,),
        )
        gcp.execute_api_request(_context, req)
    elif _is_azure_path(path):
        account, container, blob = azure.split_path(path)
        req = Request(
            url=azure.build_url(
                account, "/{container}/{blob}", container=container, blob=blob
            ),
            method="DELETE",
            success_codes=(202,),
        )
        azure.execute_api_request(_context, req)
    else:
        raise Error(f"Unrecognized path: '{path}'")


def stat(path: str) -> Stat:
    """
    Stat a file or object representing a directory, returns a Stat object
    """
    if _is_local_path(path):
        s = os.stat(path)
        return Stat(
            size=s.st_size, mtime=s.st_mtime, ctime=s.st_ctime, md5=None, version=None
        )
    elif _is_gcp_path(path):
        st = _gcp_maybe_stat(path)
        if st is None:
            raise FileNotFoundError(f"No such file: '{path}'")
        return st
    elif _is_azure_path(path):
        st = _azure_maybe_stat(path)
        if st is None:
            raise FileNotFoundError(f"No such file: '{path}'")
        return st
    else:
        raise Error(f"Unrecognized path: '{path}'")


def set_mtime(path: str, mtime: float, version: Optional[str] = None) -> bool:
    """
    Set the mtime for a path, returns True on success

    A version can be specified (as returned by `stat()`) to only update the mtime if the
    version matches
    """
    if _is_local_path(path):
        assert version is None
        os.utime(path, times=(mtime, mtime))
        return True
    elif _is_gcp_path(path):
        bucket, blob = gcp.split_path(path)
        params = None
        if version is not None:
            params = dict(ifGenerationMatch=version)
        req = Request(
            url=gcp.build_url(
                "/storage/v1/b/{bucket}/o/{object}", bucket=bucket, object=blob
            ),
            method="PATCH",
            params=params,
            data=dict(metadata={"blobfile-mtime": str(mtime)}),
            success_codes=(200, 404, 412),
        )
        resp = gcp.execute_api_request(_context, req)
        if resp.status == 404:
            raise FileNotFoundError(f"No such file: '{path}'")
        return resp.status == 200
    elif _is_azure_path(path):
        account, container, blob = azure.split_path(path)
        headers = {}
        if version is not None:
            headers["If-Match"] = version
        req = Request(
            url=azure.build_url(
                account, "/{container}/{blob}", container=container, blob=blob
            ),
            method="HEAD",
            params=dict(comp="metadata"),
            headers=headers,
            success_codes=(200, 404, 412),
        )
        resp = azure.execute_api_request(_context, req)
        if resp.status == 404:
            raise FileNotFoundError(f"No such file: '{path}'")
        if resp.status == 412:
            return False

        headers = {k: v for k, v in resp.headers.items() if k.startswith("x-ms-meta-")}
        headers["x-ms-meta-blobfilemtime"] = str(mtime)
        if version is not None:
            headers["If-Match"] = version
        req = Request(
            url=azure.build_url(
                account, "/{container}/{blob}", container=container, blob=blob
            ),
            method="PUT",
            params=dict(comp="metadata"),
            headers=headers,
            success_codes=(200, 404, 412),
        )
        resp = azure.execute_api_request(_context, req)
        if resp.status == 404:
            raise FileNotFoundError(f"No such file: '{path}'")
        return resp.status == 200
    else:
        raise Error(f"Unrecognized path: '{path}'")


def rmtree(path: str) -> None:
    """
    Delete a directory tree
    """
    if not isdir(path):
        raise NotADirectoryError(f"The directory name is invalid: '{path}'")

    if _is_local_path(path):
        shutil.rmtree(path)
    elif _is_gcp_path(path):
        if not path.endswith("/"):
            path += "/"
        bucket, blob = gcp.split_path(path)
        for entry in _gcp_list_blobs(path):
            entry_slash_path = _get_slash_path(entry)
            entry_bucket, entry_blob = gcp.split_path(entry_slash_path)
            assert entry_bucket == bucket and entry_blob.startswith(blob)
            req = Request(
                url=gcp.build_url(
                    "/storage/v1/b/{bucket}/o/{object}",
                    bucket=bucket,
                    object=entry_blob,
                ),
                method="DELETE",
                # 404 is allowed in case a failed request successfully deleted the file
                # before erroring out
                success_codes=(204, 404),
            )
            gcp.execute_api_request(_context, req)
    elif _is_azure_path(path):
        if not path.endswith("/"):
            path += "/"
        account, container, blob = azure.split_path(path)
        for entry in _azure_list_blobs(path):
            entry_slash_path = _get_slash_path(entry)
            entry_account, entry_container, entry_blob = azure.split_path(
                entry_slash_path
            )
            assert (
                entry_account == account
                and entry_container == container
                and entry_blob.startswith(blob)
            )
            req = Request(
                url=azure.build_url(
                    account, "/{container}/{blob}", container=container, blob=entry_blob
                ),
                method="DELETE",
                # 404 is allowed in case a failed request successfully deleted the file
                # before erroring out
                success_codes=(202, 404),
            )
            azure.execute_api_request(_context, req)
    else:
        raise Error(f"Unrecognized path: '{path}'")


def walk(
    top: str, topdown: bool = True, onerror: Optional[Callable[[OSError], None]] = None
) -> Iterator[Tuple[str, Sequence[str], Sequence[str]]]:
    """
    Walk a directory tree in a similar manner to os.walk
    """
    if not isdir(top):
        return

    if _is_local_path(top):
        top = os.path.normpath(top)
        for root, dirnames, filenames in os.walk(
            top=top, topdown=topdown, onerror=onerror
        ):
            assert isinstance(root, str)
            if root.endswith(os.sep):
                root = root[:-1]
            yield (root, sorted(dirnames), sorted(filenames))
    elif _is_gcp_path(top) or _is_azure_path(top):
        top = _normalize_path(top)
        if not top.endswith("/"):
            top += "/"
        if topdown:
            dq: collections.deque[str] = collections.deque()
            dq.append(top)
            while len(dq) > 0:
                cur = dq.popleft()
                assert cur.endswith("/")
                if _is_gcp_path(top):
                    it = _gcp_list_blobs(cur, delimiter="/")
                elif _is_azure_path(top):
                    it = _azure_list_blobs(cur, delimiter="/")
                else:
                    raise Error(f"Unrecognized path: '{top}'")
                dirnames = []
                filenames = []
                for entry in it:
                    entry_path = _get_slash_path(entry)
                    if entry_path == cur:
                        continue
                    if entry.is_dir:
                        dirnames.append(entry.name)
                    else:
                        filenames.append(entry.name)
                yield (_strip_slash(cur), dirnames, filenames)
                dq.extend(join(cur, dirname) + "/" for dirname in dirnames)
        else:
            if _is_gcp_path(top):
                it = _gcp_list_blobs(top)
            elif _is_azure_path(top):
                it = _azure_list_blobs(top)
            else:
                raise Error(f"Unrecognized path: '{top}'")

            cur = []
            dirnames_stack = [[]]
            filenames_stack = [[]]
            for entry in it:
                entry_slash_path = _get_slash_path(entry)
                if entry_slash_path == top:
                    continue
                relpath = entry_slash_path[len(top) :]
                parts = relpath.split("/")
                dirpath = parts[:-1]
                if dirpath != cur:
                    # pop directories from the current path until we match the prefix of this new path
                    while cur != dirpath[: len(cur)]:
                        yield (
                            top + "/".join(cur),
                            dirnames_stack.pop(),
                            filenames_stack.pop(),
                        )
                        cur.pop()
                    # push directories from the new path until the current path matches it
                    while cur != dirpath:
                        dirname = dirpath[len(cur)]
                        cur.append(dirname)
                        filenames_stack.append([])
                        # add this to child dir to the list of dirs for the parent
                        dirnames_stack[-1].append(dirname)
                        dirnames_stack.append([])
                if entry.is_file:
                    filenames_stack[-1].append(entry.name)
            while len(cur) > 0:
                yield (top + "/".join(cur), dirnames_stack.pop(), filenames_stack.pop())
                cur.pop()
            yield (_strip_slash(top), dirnames_stack.pop(), filenames_stack.pop())
            assert len(dirnames_stack) == 0 and len(filenames_stack) == 0
    else:
        raise Error(f"Unrecognized path: '{top}'")


def dirname(path: str) -> str:
    """
    Get the directory name of the path

    On GCS, the root directory is gs://<bucket name>/
    On Azure Storage, the root directory is https://<account>.blob.core.windows.net/<container>/
    """
    if _is_gcp_path(path):
        bucket, obj = gcp.split_path(path)
        obj = _strip_slashes(obj)
        if "/" in obj:
            obj = "/".join(obj.split("/")[:-1])
            return gcp.combine_path(bucket, obj)
        else:
            return gcp.combine_path(bucket, "")[:-1]
    elif _is_azure_path(path):
        account, container, obj = azure.split_path(path)
        obj = _strip_slashes(obj)
        if "/" in obj:
            obj = "/".join(obj.split("/")[:-1])
            return _azure_combine_path(account, container, obj)
        else:
            return _azure_combine_path(account, container, "")[:-1]
    else:
        return os.path.dirname(path)


def join(a: str, *args: str) -> str:
    """
    Join file paths, if a path is an absolute path, it will replace the entire path component of previous paths
    """
    out = a
    for b in args:
        out = _join2(out, b)
    return out


def _safe_urljoin(a: str, b: str) -> str:
    # a ":" symbol in a relative url path will be interpreted as a fully qualified path
    # escape the ":" to avoid this
    # https://stackoverflow.com/questions/55202875/python-urllib-parse-urljoin-on-path-starting-with-numbers-and-colon
    if ESCAPED_COLON in b:
        raise Error(f"url cannot contain string '{ESCAPED_COLON}'")
    escaped_b = b.replace(":", ESCAPED_COLON)
    joined = urllib.parse.urljoin(a, escaped_b)
    return joined.replace(ESCAPED_COLON, ":")


def _join2(a: str, b: str) -> str:
    if _is_local_path(a):
        return os.path.join(a, b)
    elif _is_gcp_path(a) or _is_azure_path(a):
        if not a.endswith("/"):
            a += "/"

        if _is_gcp_path(a):
            bucket, obj = gcp.split_path(a)
            obj = _safe_urljoin(obj, b)
            if obj.startswith("/"):
                obj = obj[1:]
            return gcp.combine_path(bucket, obj)
        elif _is_azure_path(a):
            account, container, obj = azure.split_path(a)
            obj = _safe_urljoin(obj, b)
            if obj.startswith("/"):
                obj = obj[1:]
            return _azure_combine_path(account, container, obj)
        else:
            raise Error(f"Unrecognized path: '{a}'")
    else:
        raise Error(f"Unrecognized path: '{a}'")


def get_url(path: str) -> Tuple[str, Optional[float]]:
    """
    Get a URL for the given path that a browser could open
    """
    if _is_gcp_path(path):
        bucket, blob = gcp.split_path(path)
        return gcp.generate_signed_url(bucket, blob, expiration=gcp.MAX_EXPIRATION)
    elif _is_azure_path(path):
        account, container, blob = azure.split_path(path)
        url = azure.build_url(
            account, "/{container}/{blob}", container=container, blob=blob
        )
        token = azure.sas_token_manager.get_token(
            ctx=_context, key=(account, container)
        )
        if token is None:
            # the container has public access
            return url, float("inf")
        return azure.generate_signed_url(key=token, url=url)
    elif _is_local_path(path):
        return f"file://{path}", None
    else:
        raise Error(f"Unrecognized path: '{path}'")


def _block_md5(f: BinaryIO) -> bytes:
    m = hashlib.md5()
    while True:
        block = f.read(CHUNK_SIZE)
        if block == b"":
            break
        m.update(block)
    return m.digest()


def _azure_maybe_update_md5(path: str, etag: str, hexdigest: str) -> bool:
    account, container, blob = azure.split_path(path)
    req = Request(
        url=azure.build_url(
            account, "/{container}/{blob}", container=container, blob=blob
        ),
        method="HEAD",
        headers={"If-Match": etag},
        success_codes=(200, 404, 412),
    )
    resp = azure.execute_api_request(_context, req)
    if resp.status in (404, 412):
        return False

    # these will be cleared if not provided, there does not appear to be a PATCH method like for GCS
    # https://docs.microsoft.com/en-us/rest/api/storageservices/set-blob-properties#remarks
    headers: Dict[str, str] = {}
    for src, dst in AZURE_RESPONSE_HEADER_TO_REQUEST_HEADER.items():
        if src in resp.headers:
            headers[dst] = resp.headers[src]
    headers["x-ms-blob-content-md5"] = base64.b64encode(
        binascii.unhexlify(hexdigest)
    ).decode("utf8")

    req = Request(
        url=azure.build_url(
            account, "/{container}/{blob}", container=container, blob=blob
        ),
        method="PUT",
        params=dict(comp="properties"),
        headers={
            **headers,
            # https://docs.microsoft.com/en-us/rest/api/storageservices/specifying-conditional-headers-for-blob-service-operations
            "If-Match": etag,
        },
        success_codes=(200, 404, 412),
    )
    resp = azure.execute_api_request(_context, req)
    return resp.status == 200


def _gcp_maybe_update_md5(path: str, generation: str, hexdigest: str) -> bool:
    bucket, blob = gcp.split_path(path)
    req = Request(
        url=gcp.build_url(
            "/storage/v1/b/{bucket}/o/{object}", bucket=bucket, object=blob
        ),
        method="PATCH",
        params=dict(ifGenerationMatch=generation),
        # it looks like we can't set the underlying md5Hash, only the metadata fields
        data=dict(metadata={"md5": hexdigest}),
        success_codes=(200, 404, 412),
    )

    resp = gcp.execute_api_request(_context, req)
    return resp.status == 200


def md5(path: str) -> str:
    """
    Get the MD5 hash for a file in hexdigest format.

    For GCS this will look up the MD5 in the blob's metadata, unless it's a composite object, in which case
    it must be calculated by downloading the file.
    For Azure this can look up the MD5 if it's available, otherwise it must calculate it.
    For local paths, this must always calculate the MD5.
    """
    if _is_gcp_path(path):
        st = _gcp_maybe_stat(path)
        if st is None:
            raise FileNotFoundError(f"No such file: '{path}'")

        h = st.md5
        if h is not None:
            return h

        # this is probably a composite object, calculate the md5 and store it on the file if the file has not changed
        with BlobFile(path, "rb") as f:
            result = _block_md5(f).hex()

        assert st.version is not None
        _gcp_maybe_update_md5(path, st.version, result)
        return result
    elif _is_azure_path(path):
        st = _azure_maybe_stat(path)
        if st is None:
            raise FileNotFoundError(f"No such file: '{path}'")
        # https://docs.microsoft.com/en-us/rest/api/storageservices/get-blob-properties
        h = st.md5
        if h is None:
            # md5 is missing, calculate it and store it on file if the file has not changed
            with BlobFile(path, "rb") as f:
                h = _block_md5(f).hex()
            assert st.version is not None
            _azure_maybe_update_md5(path, st.version, h)
        return h
    else:
        with BlobFile(path, "rb") as f:
            return _block_md5(f).hex()


class _StreamingReadFile(io.RawIOBase):
    def __init__(self, path: str, size: int) -> None:
        super().__init__()
        self._size = size
        self._path = path
        # current reading byte offset in the file
        self._offset = 0
        self._f = None
        self.requests = 0
        self.failures = 0
        self.bytes_read = 0

    def _request_chunk(
        self, streaming: bool, start: int, end: Optional[int] = None
    ) -> urllib3.response.HTTPResponse:
        raise NotImplementedError

    def readall(self) -> bytes:
        # https://github.com/christopher-hesse/blobfile/issues/46
        # due to a limitation of the ssl module, we cannot read more than 2**31 bytes at a time
        # reading a huge file in a single request is probably a bad idea anyway since the request
        # cannot be retried without re-reading the entire requested amount
        # instead, read into a buffer and return the buffer
        pieces = []
        while True:
            bytes_remaining = self._size - self._offset
            assert bytes_remaining >= 0, "read more bytes than expected"
            # if a user doesn't like this value, it is easy to use .read(size) directly
            opt_piece = self.read(min(CHUNK_SIZE, bytes_remaining))
            assert opt_piece is not None, "file is in non-blocking mode"
            piece = opt_piece
            if len(piece) == 0:
                break
            pieces.append(piece)
        return b"".join(pieces)

    # https://bugs.python.org/issue27501
    def readinto(self, b: Any) -> Optional[int]:
        bytes_remaining = self._size - self._offset
        if bytes_remaining <= 0:
            return 0

        if len(b) > bytes_remaining:
            # if we get a file that was larger than we expected, don't read the extra data
            b = b[:bytes_remaining]

        n = 0  # for pyright
        if USE_STREAMING_READ_REQUEST:
            for attempt, backoff in enumerate(common.exponential_sleep_generator()):
                if self._f is None:
                    resp = self._request_chunk(streaming=True, start=self._offset)
                    if resp.status == 416:
                        # likely the file was truncated while we were reading it
                        # return an empty string
                        return 0
                    self._f = resp
                    self.requests += 1

                err = None
                try:
                    opt_n = self._f.readinto(b)
                    assert opt_n is not None, "file is in non-blocking mode"
                    n = opt_n
                    if n == 0:
                        # assume that the connection has died
                        # if the file was truncated, we'll try to open it again and end up
                        # returning out of this loop
                        err = Error(
                            f"failed to read from connection while reading file at {self._path}"
                        )
                    else:
                        # only break out if we successfully read at least one byte
                        break
                except (
                    urllib3.exceptions.ReadTimeoutError,  # haven't seen this error here, but seems possible
                    urllib3.exceptions.ProtocolError,
                    urllib3.exceptions.SSLError,
                    ssl.SSLError,
                ) as e:
                    err = Error(f"exception {e} while reading file at {self._path}")
                # assume that the connection has died or is in an unusable state
                # we don't want to put a broken connection back in the pool
                # so don't call self._f.release_conn()
                self._f.close()
                self._f = None
                self.failures += 1

                if _context.retry_limit is not None and attempt >= _context.retry_limit:
                    raise err

                if attempt >= _context.retry_log_threshold:
                    _context.log_callback(
                        f"error {err} when executing readinto({len(b)}) at offset {self._offset} attempt {attempt}, sleeping for {backoff:.1f} seconds before retrying"
                    )
                time.sleep(backoff)
        else:
            resp = self._request_chunk(
                streaming=False, start=self._offset, end=self._offset + len(b)
            )
            if resp.status == 416:
                # likely the file was truncated while we were reading it
                # return an empty string
                return 0
            self.requests += 1
            n = len(resp.data)
            b[:n] = resp.data
        self.bytes_read += n
        self._offset += n
        return n

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            new_offset = offset
        elif whence == io.SEEK_CUR:
            new_offset = self._offset + offset
        elif whence == io.SEEK_END:
            new_offset = self._size + offset
        else:
            raise ValueError(
                f"Invalid whence ({whence}, should be {io.SEEK_SET}, {io.SEEK_CUR}, or {io.SEEK_END})"
            )
        if new_offset != self._offset:
            self._offset = new_offset
            if self._f is not None:
                self._f.close()
            self._f = None
        return self._offset

    def tell(self) -> int:
        return self._offset

    def close(self) -> None:
        if self.closed:
            return

        if hasattr(self, "_f") and self._f is not None:
            # normally we would return the connection to the pool at this point, but in rare
            # circumstances this can cause an invalid socket to be in the connection pool and
            # crash urllib3
            # https://github.com/urllib3/urllib3/issues/1878
            self._f.close()
            self._f = None

        super().close()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True


class _GoogleStreamingReadFile(_StreamingReadFile):
    def __init__(self, path: str) -> None:
        st = _gcp_maybe_stat(path)
        if st is None:
            raise FileNotFoundError(f"No such file or bucket: '{path}'")
        super().__init__(path, st.size)

    def _request_chunk(
        self, streaming: bool, start: int, end: Optional[int] = None
    ) -> urllib3.response.HTTPResponse:
        bucket, name = gcp.split_path(self._path)
        req = Request(
            url=gcp.build_url(
                "/storage/v1/b/{bucket}/o/{name}", bucket=bucket, name=name
            ),
            method="GET",
            params=dict(alt="media"),
            headers={"Range": _calc_range(start=start, end=end)},
            success_codes=(206, 416),
            # if we are streaming the data, make
            # sure we don't preload it
            preload_content=not streaming,
        )
        return gcp.execute_api_request(_context, req)


class _AzureStreamingReadFile(_StreamingReadFile):
    def __init__(self, path: str) -> None:
        st = _azure_maybe_stat(path)
        if st is None:
            raise FileNotFoundError(f"No such file or directory: '{path}'")
        super().__init__(path, st.size)

    def _request_chunk(
        self, streaming: bool, start: int, end: Optional[int] = None
    ) -> urllib3.response.HTTPResponse:
        account, container, blob = azure.split_path(self._path)
        req = Request(
            url=azure.build_url(
                account, "/{container}/{blob}", container=container, blob=blob
            ),
            method="GET",
            headers={"Range": _calc_range(start=start, end=end)},
            success_codes=(206, 416),
            # if we are streaming the data, make
            # sure we don't preload it
            preload_content=not streaming,
        )
        resp = azure.execute_api_request(_context, req)
        return resp


class _StreamingWriteFile(io.BufferedIOBase):
    def __init__(self, chunk_size: int) -> None:
        # current writing byte offset in the file
        self._offset = 0
        # contents waiting to be uploaded
        self._buf = b""
        self._chunk_size = chunk_size

    def _upload_chunk(self, chunk: bytes, finalize: bool) -> None:
        raise NotImplementedError

    def _upload_buf(self, finalize: bool = False):
        if finalize:
            size = len(self._buf)
        else:
            size = (len(self._buf) // self._chunk_size) * self._chunk_size
            assert size > 0
        chunk = self._buf[:size]
        self._buf = self._buf[size:]

        self._upload_chunk(chunk, finalize)
        self._offset += len(chunk)

    def close(self) -> None:
        if self.closed:
            return

        # we will have a partial remaining buffer at this point
        self._upload_buf(finalize=True)
        super().close()

    def tell(self) -> int:
        return self._offset

    def writable(self) -> bool:
        return True

    def write(self, b: bytes) -> int:
        self._buf += b
        while len(self._buf) > self._chunk_size:
            self._upload_buf()
        return len(b)

    def readinto(self, b: Any) -> int:
        raise io.UnsupportedOperation("not readable")

    def detach(self) -> io.RawIOBase:
        raise io.UnsupportedOperation("no underlying raw stream")

    def read1(self, size: int = -1) -> bytes:
        raise io.UnsupportedOperation("not readable")

    def readinto1(self, b: Any) -> int:
        raise io.UnsupportedOperation("not readable")


class _GoogleStreamingWriteFile(_StreamingWriteFile):
    def __init__(self, path: str) -> None:
        bucket, name = gcp.split_path(path)
        req = Request(
            url=gcp.build_url(
                "/upload/storage/v1/b/{bucket}/o?uploadType=resumable", bucket=bucket
            ),
            method="POST",
            data=dict(name=name),
            success_codes=(200, 400, 404),
        )
        resp = gcp.execute_api_request(_context, req)
        if resp.status in (400, 404):
            raise FileNotFoundError(f"No such file or bucket: '{path}'")
        self._upload_url = resp.headers["Location"]
        # https://cloud.google.com/storage/docs/json_api/v1/how-tos/resumable-upload
        assert _context.google_write_chunk_size % (256 * 1024) == 0
        super().__init__(chunk_size=_context.google_write_chunk_size)

    def _upload_chunk(self, chunk: bytes, finalize: bool) -> None:
        start = self._offset
        end = self._offset + len(chunk) - 1

        total_size = "*"
        if finalize:
            total_size = self._offset + len(chunk)
            assert len(self._buf) == 0

        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end}/{total_size}",
        }
        if len(chunk) == 0 and finalize:
            # this is not mentioned in the docs but appears to be allowed
            # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Range
            headers["Content-Range"] = f"bytes */{total_size}"

        req = Request(
            url=self._upload_url,
            data=chunk,
            headers=headers,
            method="PUT",
            success_codes=(200, 201) if finalize else (308,),
        )

        try:
            gcp.execute_api_request(_context, req)
        except RequestFailure as e:
            # https://cloud.google.com/storage/docs/resumable-uploads#practices
            if e.response_status in (404, 410):
                raise RestartableStreamingWriteFailure(
                    message=e.message,
                    request_string=e.request_string,
                    response_status=e.response_status,
                    error=e.error,
                    error_description=e.error_description,
                )
            else:
                raise


def _clear_uncommitted_blocks(url: str, metadata: Dict[str, str]) -> None:
    # to avoid leaking uncommitted blocks, we can do a Put Block List with
    # all the existing blocks for a file
    # this will change the last-modified timestamp and the etag
    req = Request(
        url=url, params=dict(comp="blocklist"), method="GET", success_codes=(200, 404)
    )
    resp = azure.execute_api_request(_context, req)
    if resp.status != 200:
        return

    result = xmltodict.parse(resp.data)
    if result["BlockList"]["CommittedBlocks"] is None:
        return

    blocks = result["BlockList"]["CommittedBlocks"]["Block"]
    if isinstance(blocks, dict):
        blocks = [blocks]

    body = {"BlockList": {"Latest": [b["Name"] for b in blocks]}}
    # make sure to preserve metadata for the file
    headers: Dict[str, str] = {
        k: v for k, v in metadata.items() if k.startswith("x-ms-meta-")
    }
    for src, dst in AZURE_RESPONSE_HEADER_TO_REQUEST_HEADER.items():
        if src in metadata:
            headers[dst] = metadata[src]
    req = Request(
        url=url,
        method="PUT",
        params=dict(comp="blocklist"),
        headers={**headers, "If-Match": metadata["etag"]},
        data=body,
        success_codes=(201, 404, 412),
    )
    azure.execute_api_request(_context, req)


class _AzureStreamingWriteFile(_StreamingWriteFile):
    def __init__(self, path: str) -> None:
        self._path = path
        account, container, blob = azure.split_path(path)
        self._url = azure.build_url(
            account, "/{container}/{blob}", container=container, blob=blob
        )
        # block blobs let you upload up to 100,000 "uncommitted" blocks with user-chosen block ids
        # using the "Put Block" call
        # you may then call "Put Block List" with up to 50,000 block ids of the blocks you
        # want to be in the blob (50,000 is the max blocks per blob)
        # all unused uncommitted blocks will be deleted
        # uncommitted blocks also expire after a week if they are not committed
        #
        # since we use block blobs, there are a few ways we could implement this streaming write file
        #
        # method 1:
        #   upload the first chunk of the file as block id "0", the second as block id "1" etc
        #   when we are done writing the file, we call "Put Block List" using range(num_blocks) as
        #   the block ids
        #
        #   this has the advantage that if our program crashes, the same block ids will be reused
        #   for the next upload and so we'll never get more than 50,000 uncommitted blocks
        #
        #   in general, azure does not seem to support concurrent writers except maybe
        #   for writing small files (GCS does to a limited extent through resumable upload sessions)
        #
        #   with method 1, if you have two writers:
        #
        #       writer 0: write block id "0"
        #       writer 1: write block id "0"
        #       writer 1: crash
        #       writer 0: write block id "1"
        #       writer 0: put block list ["0", "1"]
        #
        #   then you will end up with block "0" from writer 1 and block "1" from writer 0, which means
        #   your file will be corrupted
        #
        #   this appears to be the method used by the azure python SDK
        #
        # method 2:
        #   generate a random session id
        #   upload the first chunk of the file as block id "<session id>-0",
        #       the second block as "<session id>-1" etc
        #   when we are done writing the file, call "Put Block List" using
        #       [f"<session id>-{i}" for i in range(num_blocks)] as the block list
        #
        #   this has the advantage that we should not observe data corruption from concurrent writers
        #       assuming that the session ids are unique, although whichever writer finishes first will
        #       win, because calling "Put Block List" will delete all uncommitted blocks
        #
        #   this has the disadvantage that we can end up hitting the uncommitted block limit
        #       1) with 100,000 concurrent writers, each one would write the first block, then all
        #           would immediately hit the block limit and get 409 errors
        #       2) with a single writer that crashes every time it writes the second block, it would
        #           retry 100,000 times, then be unable to continue due to all the uncommitted blocks
        #           it was generating
        #
        #   the workaround we use here is that whenever a file is opened for reading, we clear all
        #       uncommitted blocks by calling "Put Block List" with the list of currently committed blocks
        #
        #   this seems to be reasonably fast in practice, and means that failure #2 should not be an issue
        #
        #   failure #1 could still happen with concurrent writers, but this should result only in a
        #       confusing error message (409 error) instead of a ConcurrentWriteFailure, though we
        #       could likely raise that error if we saw a 409 with the error RequestEntityTooLargeBlockCountExceedsLimit
        #
        #   this does change the behavior slightly, now the writer that will end up succeeding on "Put Block List"
        #       is likely to be the last writer to open the file for writing, the others will fail
        #       because their uncommitted blocks have been cleared
        #
        # it would be nice to replace this with a less odd method, but it's not obvious how
        #   to do this on azure storage
        #
        # if there were upload sessions like GCS, this wouldn't be an issue
        # if there was no uncommitted block limit, method 2 would work fine
        # if blobs could automatically expire without having to add a container lifecycle rule
        #   then we could upload to a temp path, then copy to the final path (assuming copy is atomic)
        #   without automatic expiry, we'd leak temp files
        # we can use the lease system, but then we have to deal with leases

        self._upload_id = random.randint(0, 2 ** 47 - 1)
        self._block_index = 0
        # check to see if there is an existing blob at this location with the wrong type
        req = Request(
            url=self._url,
            method="HEAD",
            success_codes=(200, 400, 404, INVALID_HOSTNAME_STATUS),
        )
        resp = azure.execute_api_request(_context, req)
        if resp.status == 200:
            if resp.headers["x-ms-blob-type"] == "BlockBlob":
                # because we delete all the uncommitted blocks, any concurrent writers will fail
                # but they would fail anyway since the first writer to finish would end up
                # deleting all uncommitted blocks
                # this means that the last writer to start is likely to win, the others should fail
                # with ConcurrentWriteFailure
                _clear_uncommitted_blocks(self._url, resp.headers)
            else:
                # if the existing blob type is not compatible with the block blob we are about to write
                # we have to delete the file before writing our block blob or else we will get a 409
                # error when putting the first block
                remove(path)
        elif resp.status in (400, INVALID_HOSTNAME_STATUS) or (
            resp.status == 404
            and resp.headers["x-ms-error-code"] == "ContainerNotFound"
        ):
            raise FileNotFoundError(
                f"No such file or container/account does not exist: '{path}'"
            )
        self._md5 = hashlib.md5()
        super().__init__(chunk_size=_context.azure_write_chunk_size)

    def _upload_chunk(self, chunk: bytes, finalize: bool) -> None:
        start = 0
        while start < len(chunk):
            # premium block blob storage supports block blobs and append blobs
            # https://azure.microsoft.com/en-us/blog/azure-premium-block-blob-storage-is-now-generally-available/
            # we use block blobs because they are compatible with WASB:
            # https://docs.microsoft.com/en-us/azure/databricks/kb/data-sources/wasb-check-blob-types
            end = start + _context.azure_write_chunk_size
            data = chunk[start:end]
            self._md5.update(data)
            req = Request(
                url=self._url,
                method="PUT",
                params=dict(
                    comp="block",
                    blockid=_azure_block_index_to_block_id(
                        self._block_index, self._upload_id
                    ),
                ),
                data=data,
                success_codes=(201,),
            )
            azure.execute_api_request(_context, req)
            self._block_index += 1
            if self._block_index >= AZURE_BLOCK_COUNT_LIMIT:
                raise Error(
                    f"Exceeded block count limit of {AZURE_BLOCK_COUNT_LIMIT} for Azure Storage.  Increase `azure_write_chunk_size` so that {AZURE_BLOCK_COUNT_LIMIT} * `azure_write_chunk_size` exceeds the size of the file you are writing."
                )

            start += _context.azure_write_chunk_size

        if finalize:
            block_ids = [
                _azure_block_index_to_block_id(i, self._upload_id)
                for i in range(self._block_index)
            ]
            _azure_finalize_blob(
                path=self._path,
                url=self._url,
                block_ids=block_ids,
                md5_digest=self._md5.digest(),
            )


@overload
def BlobFile(
    path: str,
    mode: Literal["rb", "wb", "ab"],
    streaming: Optional[bool] = ...,
    buffer_size: int = ...,
    cache_dir: Optional[str] = ...,
) -> BinaryIO:
    ...


@overload
def BlobFile(
    path: str,
    mode: Literal["r", "w", "a"] = ...,
    streaming: Optional[bool] = ...,
    buffer_size: int = ...,
    cache_dir: Optional[str] = ...,
) -> TextIO:
    ...


def BlobFile(
    path: str,
    mode: Literal["r", "rb", "w", "wb", "a", "ab"] = "r",
    streaming: Optional[bool] = None,
    buffer_size: int = io.DEFAULT_BUFFER_SIZE,
    cache_dir: Optional[str] = None,
):
    """
    Open a local or remote file for reading or writing

    Args:
        path local or remote path
        mode: one of "r", "rb", "w", "wb", "a", "ab" indicating the mode to open the file in
        streaming: the default for `streaming` is `True` when `mode` is in `"r", "rb"` and `False` when `mode` is in `"w", "wb", "a", "ab"`.
            * `streaming=True`:
                * Reading is done without downloading the entire remote file.
                * Writing is done to the remote file directly, but only in chunks of a few MB in size.  `flush()` will not cause an early write.
                * Appending is not implemented.
            * `streaming=False`:
                * Reading is done by downloading the remote file to a local file during the constructor.
                * Writing is done by uploading the file on `close()` or during destruction.
                * Appending is done by downloading the file during construction and uploading on `close()` or during destruction.
        buffer_size: number of bytes to buffer, this can potentially make reading more efficient.
        cache_dir: a directory in which to cache files for reading, only valid if `streaming=False` and `mode` is in `"r", "rb"`.   You are reponsible for cleaning up the cache directory.

    Returns:
        A file-like object
    """
    if _guess_isdir(path):
        raise IsADirectoryError(f"Is a directory: '{path}'")

    if streaming is None:
        streaming = mode in ("r", "rb")

    if _is_local_path(path) and "w" in mode:
        # local filesystems require that intermediate directories exist, but this is not required by the
        # remote filesystems
        # for consistency, automatically create local intermediate directories
        if dirname(path) != "":
            makedirs(dirname(path))

    if streaming:
        if mode not in ("w", "wb", "r", "rb"):
            raise Error(f"Invalid mode for streaming file: '{mode}'")
        if cache_dir is not None:
            raise Error("Cannot specify cache_dir for streaming files")
        if _is_local_path(path):
            f = io.FileIO(path, mode=mode)
            if "r" in mode:
                f = io.BufferedReader(f, buffer_size=buffer_size)
            else:
                f = io.BufferedWriter(f, buffer_size=buffer_size)
        elif _is_gcp_path(path):
            if mode in ("w", "wb"):
                f = _GoogleStreamingWriteFile(path)
            elif mode in ("r", "rb"):
                f = _GoogleStreamingReadFile(path)
                f = io.BufferedReader(f, buffer_size=buffer_size)
            else:
                raise Error(f"Unsupported mode: '{mode}'")
        elif _is_azure_path(path):
            if mode in ("w", "wb"):
                f = _AzureStreamingWriteFile(path)
            elif mode in ("r", "rb"):
                f = _AzureStreamingReadFile(path)
                f = io.BufferedReader(f, buffer_size=buffer_size)
            else:
                raise Error(f"Unsupported mode: '{mode}'")
        else:
            raise Error(f"Unrecognized path: '{path}'")

        # this should be a protocol so we don't have to cast
        # but the standard library does not seem to have a file-like protocol
        binary_f = cast(BinaryIO, f)
        if "b" in mode:
            return binary_f
        else:
            text_f = io.TextIOWrapper(binary_f, encoding="utf8")
            # TextIOWrapper bypasses buffering on purpose: https://bugs.python.org/issue13393
            # Example: https://gist.github.com/christopher-hesse/b4aab4f6f9bcba597d079f3363dfab2c
            #
            # This happens when TextIOWrapper calls f.read1(CHUNK_SIZE)
            # https://github.com/python/cpython/blob/3d17c045b4c3d09b72bbd95ed78af1ae6f0d98d2/Modules/_io/textio.c#L1854
            # and BufferedReader only reads the requested size, not the buffer_size
            # https://github.com/python/cpython/blob/8666356280084f0426c28a981341f72eaaacd006/Modules/_io/bufferedio.c#L945
            #
            # The workaround appears to be to set the _CHUNK_SIZE property or monkey patch binary_f.read1 to call binary_f.read
            if hasattr(text_f, "_CHUNK_SIZE"):
                setattr(text_f, "_CHUNK_SIZE", buffer_size)
            return cast(TextIO, text_f)
    else:
        remote_path = None
        tmp_dir = None
        if mode not in ("w", "wb", "r", "rb", "a", "ab"):
            raise Error(f"Invalid mode: '{mode}'")

        if cache_dir is not None and mode not in ("r", "rb"):
            raise Error("cache_dir only supported in read mode")

        local_filename = basename(path)
        if local_filename == "":
            local_filename = "local.tmp"
        if _is_gcp_path(path) or _is_azure_path(path):
            remote_path = path
            if mode in ("a", "ab"):
                tmp_dir = tempfile.mkdtemp()
                local_path = join(tmp_dir, local_filename)
                if exists(remote_path):
                    copy(remote_path, local_path)
            elif mode in ("r", "rb"):
                if cache_dir is None:
                    tmp_dir = tempfile.mkdtemp()
                    local_path = join(tmp_dir, local_filename)
                    copy(remote_path, local_path)
                else:
                    if not _is_local_path(cache_dir):
                        raise Error(f"cache_dir must be a local path: '{cache_dir}'")
                    makedirs(cache_dir)
                    path_md5 = hashlib.md5(path.encode("utf8")).hexdigest()
                    lock_path = join(cache_dir, f"{path_md5}.lock")
                    tmp_path = join(cache_dir, f"{path_md5}.tmp")
                    with filelock.FileLock(lock_path):
                        remote_version = ""
                        # get some sort of consistent remote hash so we can check for a local file
                        if _is_gcp_path(path):
                            st = _gcp_maybe_stat(path)
                            if st is None:
                                raise FileNotFoundError(f"No such file: '{path}'")
                            assert st.version is not None
                            remote_version = st.version
                            remote_hash = st.md5
                        elif _is_azure_path(path):
                            # in the azure case the remote md5 may not exist
                            # this duplicates some of md5() because we want more control
                            st = _azure_maybe_stat(path)
                            if st is None:
                                raise FileNotFoundError(f"No such file: '{path}'")
                            assert st.version is not None
                            remote_version = st.version
                            remote_hash = st.md5
                        else:
                            raise Error(f"Unrecognized path: '{path}'")

                        perform_copy = False
                        if remote_hash is None:
                            # there is no remote md5, copy the file
                            # and attempt to update the md5
                            perform_copy = True
                        else:
                            expected_local_path = join(
                                cache_dir, remote_hash, local_filename
                            )
                            perform_copy = not exists(expected_local_path)

                        if perform_copy:
                            local_hexdigest = copy(
                                remote_path, tmp_path, overwrite=True, return_md5=True
                            )
                            assert local_hexdigest is not None, "failed to return md5"
                            # the file we downloaded may not match the remote file because
                            # the remote file changed while we were downloading it
                            # in this case make sure we don't cache it under the wrong md5
                            local_path = join(
                                cache_dir, local_hexdigest, local_filename
                            )
                            os.makedirs(dirname(local_path), exist_ok=True)
                            if os.path.exists(local_path):
                                # the file is already here, nevermind
                                os.remove(tmp_path)
                            else:
                                os.replace(tmp_path, local_path)

                            if remote_hash is None:
                                if _is_azure_path(path):
                                    _azure_maybe_update_md5(
                                        path, remote_version, local_hexdigest
                                    )
                                elif _is_gcp_path(path):
                                    _gcp_maybe_update_md5(
                                        path, remote_version, local_hexdigest
                                    )
                        else:
                            assert remote_hash is not None
                            local_path = join(cache_dir, remote_hash, local_filename)
            else:
                tmp_dir = tempfile.mkdtemp()
                local_path = join(tmp_dir, local_filename)
        elif _is_local_path(path):
            local_path = path
        else:
            raise Error(f"Unrecognized path: '{path}'")

        f = _ProxyFile(
            local_path=local_path, mode=mode, tmp_dir=tmp_dir, remote_path=remote_path
        )
        if "r" in mode:
            f = io.BufferedReader(f, buffer_size=buffer_size)
        else:
            f = io.BufferedWriter(f, buffer_size=buffer_size)
        binary_f = cast(BinaryIO, f)
        if "b" in mode:
            return binary_f
        else:
            text_f = io.TextIOWrapper(binary_f, encoding="utf8")
            return cast(TextIO, text_f)


class _ProxyFile(io.FileIO):
    def __init__(
        self,
        local_path: str,
        mode: 'Literal["r", "rb", "w", "wb", "a", "ab"]',
        tmp_dir: Optional[str],
        remote_path: Optional[str],
    ) -> None:
        super().__init__(local_path, mode=mode)
        self._mode = mode
        self._tmp_dir = tmp_dir
        self._local_path = local_path
        self._remote_path = remote_path
        self._closed = False

    def close(self) -> None:
        if not hasattr(self, "_closed") or self._closed:
            return

        super().close()
        try:
            if self._remote_path is not None and self._mode in ("w", "wb", "a", "ab"):
                copy(self._local_path, self._remote_path, overwrite=True)
        finally:
            # if the copy fails, still cleanup our local temp file so it is not leaked
            if self._tmp_dir is not None:
                os.remove(self._local_path)
                os.rmdir(self._tmp_dir)
        self._closed = True
