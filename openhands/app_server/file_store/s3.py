import logging
import os
from typing import Any, TypedDict

import boto3
import botocore
from botocore.config import Config
from pydantic import Field, PrivateAttr

from openhands.app_server.file_store.files import FileStore

logger = logging.getLogger(__name__)


def _use_real_aws() -> bool:
    """Determine whether to use real AWS S3 or a local compatible service."""
    return os.getenv('USE_AWS_S3', 'false').lower() in ('true', '1')


def create_s3_client() -> Any:
    """Create a boto3 S3 client, choosing between real AWS and LocalStack.

    When ``USE_AWS_S3`` is true (or ``'1'``), the client uses the standard
    boto3 resolution chain (environment variables, ``~/.aws/credentials``,
    IAM roles, etc.) with no custom endpoint.

    When ``USE_AWS_S3`` is false (the default), the client targets a
    LocalStack-compatible service:

    * ``LOCALSTACK_ENDPOINT`` — custom endpoint URL (default: ``http://localhost:4566``)
    * ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` — plaintext test credentials
    * Path-style addressing — required because LocalStack does not resolve
      virtual-hosted-style bucket names through the custom endpoint.

    Returns:
        A configured ``boto3.client('s3')`` instance.
    """
    if _use_real_aws():
        return boto3.client('s3')

    # In Docker: LocalStack is at 'localstack:4566'. On host: 'localhost:4566'.
    # Prefer LOCALSTACK_ENDPOINT env, but fix 'localhost' when running in Docker
    # (localhost inside Docker is the container itself, not the host).
    in_docker = os.path.isdir('/app/.venv')
    endpoint = os.getenv('LOCALSTACK_ENDPOINT')
    if not endpoint:
        endpoint = 'http://localstack:4566' if in_docker else 'http://localhost:4566'
    elif in_docker and 'localhost' in endpoint:
        endpoint = endpoint.replace('localhost', 'localstack')
        logger.warning(
            '[S3FileStore] LOCALSTACK_ENDPOINT=%s contains localhost; '
            'rewriting to %s (localhost inside Docker is the container)',
            os.getenv('LOCALSTACK_ENDPOINT'), endpoint,
        )
    logger.info('[S3FileStore] Creating client with endpoint=%s', endpoint)
    return boto3.client(
        's3',
        aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID', 'test'),
        aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY', 'test'),
        endpoint_url=_ensure_url_scheme(False, endpoint),
        use_ssl=False,
        config=Config(s3={'addressing_style': 'path'}),
    )


def _ensure_url_scheme(secure: bool, url: str | None) -> str | None:
    if not url:
        return None
    if secure:
        if not url.startswith('https://'):
            url = 'https://' + url.removeprefix('http://')
    else:
        if not url.startswith('http://'):
            url = 'http://' + url.removeprefix('https://')
    return url


class S3ObjectDict(TypedDict):
    Key: str


class GetObjectOutputDict(TypedDict):
    Body: Any


class ListObjectsV2OutputDict(TypedDict):
    Contents: list[S3ObjectDict] | None


class S3FileStore(FileStore):
    """S3-compatible file store.

    The S3 client is initialized lazily on first access.  Client creation
    is delegated to :func:`create_s3_client` so that the same factory logic
    is shared across the codebase.
    """

    bucket_name: str = Field(default='')

    _client: Any = PrivateAttr(default=None)
    _resolved_bucket: str | None = PrivateAttr(default=None)
    _bucket_checked: bool = PrivateAttr(default=False)

    def _ensure_bucket(self) -> None:
        """Create bucket if it doesn't exist (no-op if it does)."""
        if self._bucket_checked:
            return
        self._bucket_checked = True
        bucket = self._get_bucket_name()
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            self.client.create_bucket(Bucket=bucket)
            logger.info('[S3FileStore] Created bucket %s', bucket)

    def _get_bucket_name(self) -> str:
        """Get bucket name, falling back to environment variable if not set."""
        if self._resolved_bucket is None:
            self._resolved_bucket = self.bucket_name or os.environ['AWS_S3_BUCKET']
        return self._resolved_bucket

    @property
    def client(self) -> Any:
        """Get the S3 client, initializing lazily on first access."""
        if self._client is None:
            self._client = create_s3_client()
        return self._client

    def write(self, path: str, contents: str | bytes) -> None:
        self._ensure_bucket()
        try:
            as_bytes = (
                contents.encode('utf-8') if isinstance(contents, str) else contents
            )
            self.client.put_object(
                Bucket=self._get_bucket_name(), Key=path, Body=as_bytes
            )
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'AccessDenied':
                raise FileNotFoundError(
                    f"Error: Access denied to bucket '{self._get_bucket_name()}'."
                )
            elif e.response['Error']['Code'] == 'NoSuchBucket':
                raise FileNotFoundError(
                    f"Error: The bucket '{self._get_bucket_name()}' does not exist."
                )
            raise FileNotFoundError(
                f"Error: Failed to write to bucket '{self._get_bucket_name()}' at path {path}: {e}"
            )

    def read(self, path: str) -> str:
        try:
            response: GetObjectOutputDict = self.client.get_object(
                Bucket=self._get_bucket_name(), Key=path
            )
            with response['Body'] as stream:
                return str(stream.read().decode('utf-8'))
        except botocore.exceptions.ClientError as e:
            # Catch all S3-related errors
            if e.response['Error']['Code'] == 'NoSuchBucket':
                raise FileNotFoundError(
                    f"Error: The bucket '{self._get_bucket_name()}' does not exist."
                )
            elif e.response['Error']['Code'] == 'NoSuchKey':
                raise FileNotFoundError(
                    f"Error: The object key '{path}' does not exist in bucket '{self._get_bucket_name()}'."
                )
            else:
                raise FileNotFoundError(
                    f"Error: Failed to read from bucket '{self._get_bucket_name()}' at path {path}: {e}"
                )
        except Exception as e:
            raise FileNotFoundError(
                f"Error: Failed to read from bucket '{self._get_bucket_name()}' at path {path}: {e}"
            )

    def list(self, path: str) -> list[str]:
        if not path or path == '/':
            path = ''
        elif not path.endswith('/'):
            path += '/'
        # The delimiter logic screens out directories, so we can't use it. :(
        # For example, given a structure:
        #   foo/bar/zap.txt
        #   foo/bar/bang.txt
        #   ping.txt
        # prefix=None, delimiter="/"   yields  ["ping.txt"]  # :(
        # prefix="foo", delimiter="/"  yields  []  # :(
        results: set[str] = set()
        prefix_len = len(path)
        response: ListObjectsV2OutputDict = self.client.list_objects_v2(
            Bucket=self._get_bucket_name(), Prefix=path
        )
        contents = response.get('Contents')
        if not contents:
            return []
        paths = [obj['Key'] for obj in contents]
        for sub_path in paths:
            if sub_path == path:
                continue
            try:
                index = sub_path.index('/', prefix_len + 1)
                if index != prefix_len:
                    results.add(sub_path[: index + 1])
            except ValueError:
                results.add(sub_path)
        return list(results)

    def delete(self, path: str) -> None:
        try:
            # Sanitize path
            if not path or path == '/':
                path = ''
            if path.endswith('/'):
                path = path[:-1]

            # Try to delete any child resources (Assume the path is a directory)
            response = self.client.list_objects_v2(
                Bucket=self._get_bucket_name(), Prefix=f'{path}/'
            )
            for content in response.get('Contents') or []:
                self.client.delete_object(
                    Bucket=self._get_bucket_name(), Key=content['Key']
                )

            # Next try to delete item as a file
            self.client.delete_object(Bucket=self._get_bucket_name(), Key=path)

        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchBucket':
                raise FileNotFoundError(
                    f"Error: The bucket '{self._get_bucket_name()}' does not exist."
                )
            elif e.response['Error']['Code'] == 'AccessDenied':
                raise FileNotFoundError(
                    f"Error: Access denied to bucket '{self._get_bucket_name()}'."
                )
            elif e.response['Error']['Code'] == 'NoSuchKey':
                raise FileNotFoundError(
                    f"Error: The object key '{path}' does not exist in bucket '{self._get_bucket_name()}'."
                )
            else:
                raise FileNotFoundError(
                    f"Error: Failed to delete key '{path}' from bucket '{self._get_bucket_name()}': {e}"
                )
        except Exception as e:
            raise FileNotFoundError(
                f"Error: Failed to delete key '{path}' from bucket '{self._get_bucket_name()}: {e}"
            )
