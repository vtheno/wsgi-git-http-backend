"""Utility functions to invoke git-http-backend
"""

import logging
import subprocess
import threading


DEFAULT_CHUNK_SIZE = 0x8000
DEFAULT_MAX_HEADER_SIZE = 0X20000  # No header should ever be this large.
# TODO: expose these sizes to API


# def flask_to_git_http_backend(git_repo_path, environ, start_response):
#     # Parse environ
#     # Ivoke run_git_http_backend
#     # Catch header
#     # start_response
#     # return payload-iterator
#     pass  # TODO


def wsgi_to_git_http_backend(git_repo_path, environ, start_response):
    # Parse environ
    # Ivoke run_git_http_backend
    # Catch header
    # start_response
    # return payload-iterator
    pass  # TODO


def run_git_http_backend(cgi_environ, input_stream, log_std_err=False):
    """Execute "git http-backend" as a CGI script, using the supplied
    environment and the file-like object input_stream.

    See build_cgi_environ() and git documentation for the requirements
    for cgi_environ .

    input_stream will normally be a StringIO object, but any object
    implementing the file protocol will work. Note that input_stream
    will not be closed here.

    Any stderr generated by the git process will be ignored unless
    log_std_err is True, in which case the output will go to the standard
    Python logging module. As usual, it is up to the application to
    configure logging if log_std_err is set.

    Return (cgi_header, response_body_generator). The cgi_header is the
    string of raw headers returned by git ending with just one '\r\n'. The
    response sent back to the client will need an additional blank line
    separating this from the response body.

    Raise EnvironmentError (errno 1) if a CGI/HTTP header is not returned
    from git http-backend."""
    if log_std_err:
        stderr = subprocess.PIPE
    else:
        stderr = None
    proc = subprocess.Popen(
        ['git', 'http-backend'],
        bufsize=DEFAULT_CHUNK_SIZE,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        env=cgi_environ
    )
    cgi_header, response_body_generator = _communicate_with_git(
        proc, input_stream, log_std_err
    )
    return cgi_header, response_body_generator


def build_cgi_environ(wsgi_environ, git_project_root, user=None):
    """Build a CGI environ from a WSGI environment:

    CONTENT_TYPE
    GIT_PROJECT_ROOT = directory containing bare repos
    PATH_INFO (if GIT_PROJECT_ROOT is set, otherwise PATH_TRANSLATED)
    QUERY_STRING
    REMOTE_USER
    REMOTE_ADDR
    REQUEST_METHOD

    The git_project_root parameter must point to a directory that contains
    the git bare repo designated by PATH_INFO. See the git documentation.

    If REMOTE_USER is set in wsgi_environ, you should normally leave user
    alone.
    """
    cgi_environ = dict(wsgi_environ)
    for key, value in cgi_environ.iteritems():
        if not isinstance(value, str):
            del cgi_environ[key]
    cgi_environ['GIT_HTTP_EXPORT_ALL'] = '1'
    cgi_environ[GIT_PROJECT_ROOT] = git_project_root
    if user:
        cgi_environ['REMOTE_USER'] = user
    cgi_environ.setdefault('REMOTE_USER', 'unknown')
    return cgi_environ


def _communicate_with_git(proc, input_stream, log_std_err):
    # Given a subprocess.Popen object:
    # * Start writing request data
    # * Start reading stdout (and possibly stderr)
    # * Extract the cgi_header
    # * Construct a generator for everything that comes after the header
    # * Return (cgi_header, response_body_generator)
    # (The generator is responsible for extracting all data and cleaning up.)
    # Raise EnvironmentError (errno 1) if header is not returned from proc.
    threading.Thread(target=_input_data_pump,
                     args=(proc, input_stream)).start()
    if not log_std_err:
        threading.Thread(target=_error_data_pump, args=(proc,)).start()
    chunks = ['']  # Dummy str at start helps here.
    header_end = None
    while not header_end:
        total_bytes_read = sum(map(len, chunks))
        if total_bytes_read > DEFAULT_MAX_HEADER_SIZE:
            raise raise EnvironmentError(
                1,
                'Read %d bytes from "git http-backend" without '
                'finding header boundary.' % total_bytes_read,
            )  # TODO: Test this.
        chuck_data = proc.stdout.read(DEFAULT_CHUNK_SIZE)
        if not chuck_data:
            raise raise EnvironmentError(
                1,
                'Did not find header boundary in response '
                'from "git http-backend".',
            )  # TODO: Test this.
        chunks.append(chuck_data)
        # Search the two most recent chunks for the end of the header.
        # header_end -> (header_end_on_boundary, index_within_chunk) or None
        header_end = _find_header_end_in_2_chunks(*chunks[-2:])
    header_end_on_boundary, index_within_chunk = header_end
    cgi_header, remainder = _separate_header(
        chunks, header_end_on_boundary, index_within_chunk
    )
    response_body_generator = _response_body_generator(remainder, proc)
    return cgi_header, response_body_generator


def _input_data_pump(proc, input_stream):
    # Thread for feeding input to git
    # TODO: Currently using threads due to lack of universal standard for
    # async event loops in parent applications.
    current_data = input_stream.read(DEFAULT_CHUNK_SIZE)
    while current_data:
        proc.stdin.write(current_data)
        current_data = input_stream.read(DEFAULT_CHUNK_SIZE)
    proc.stdin.close()


def _error_data_pump(proc):
    # Thread for logging stderr from git
    # TODO: Currently using threads due to lack of universal standard for
    # async event loops in parent applications.
    log = logging.getLogger(__name__)
    for error_message in proc.stderr:
        log.error(error_message)


def _find_header_end_in_2_chunks(chunk0, chunk1):
    # Search for the header end (b'\r\n\r\n') in either the end of the
    # first chunk (with the 4-byte boundary stretching into the second
    # chunk) or within the second chunk starting at 0. Return as
    # (header_end_on_boundary, index_within_chunk).
    # Return None if header end not found.
    boundary_string = chunk0[-3:] + chunk1[:3]
    header_end = _search_str_for_header_end(boundary_string)
    if header_end != -1:
        return True, len(chunk0) - 3 + header_end
    header_end = _search_str_for_header_end(chunk1)
    if header_end != -1:
        return False, header_end
    return None


def _search_str_for_header_end(data_str):
    """Return index of header end or -1."""
    return data_str.find(b'\r\n\r\n')


def _separate_header(chunks, header_end_on_boundary, index_within_chunk):
    # Return header, remainder
    if header_end_on_boundary:
        # Header ends within chunks[-2]
        header_chunks = chunks[:-2]
        last_header_chunk = chunks[-2]
        body_start_index = (4 - (len(last_header_chunk) - index_within_chunk))
    else:
        # Header ends within chunks[-1]
        header_chunks = chunks[:-1]
        last_header_chunk = chunks[-1]
        body_start_index = index_within_chunk + 4
    header_chunks.append(last_header_chunk[:index_within_chunk])
    header_chunks.append('\r\n')  # Line end might have been split.
    header = ''.join(header_chunks)
    remainder = chunks[-1][body_start_index:]
    return header, remainder


def _response_body_generator(remainder, proc):
    yield remainder
    current_data = proc.stdout.read(DEFAULT_CHUNK_SIZE)
    while current_data:
        yield current_data
        current_data = proc.stdout.read(DEFAULT_CHUNK_SIZE)
    # TODO: Do we need this?
    while proc.poll() is None:
        yield ''
