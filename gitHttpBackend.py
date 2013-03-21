"""Utility functions to invoke git-http-backend
"""

import subprocess
import threading


DEFAULT_CHUNK_SIZE = 0x8000
DEFAULT_MAX_HEADER_SIZE = 0X20000  # No header should ever be this large.
# TODO: expose these sizes to API
CRLF = b'\r\n'
HEADER_END = CRLF * 2


def wsgi_to_git_http_backend(wsgi_environ,
                             git_project_root,
                             user=None):
    """Convenience wrapper for how a WSGI application can use this
    module to handle a request.

    See build_cgi_environ regarding git_project_root and user.

    See run_git_http_backend for requirements for wsgi.input
    and wsgi.errors."""
    cgi_environ = build_cgi_environ(wsgi_environ, git_project_root, user)
    input_stream = wsgi_environ['wsgi.input']
    error_stream = wsgi_environ['wsgi.errors']
    cgi_header, response_body_generator = run_git_http_backend(
        cgi_environ, input_stream, error_stream
    )
    status_line, list_of_headers = parse_cgi_header(cgi_header)
    return status_line, list_of_headers, response_body_generator


def run_git_http_backend(cgi_environ, input_stream, error_stream):
    """Execute "git http-backend" as a CGI script, using the supplied
    environment and the file-like object input_stream.

    See build_cgi_environ() and git documentation for the requirements
    for cgi_environ .

    input_stream can be any object implementing the file protocol. Note
    that input_stream will be closed here.

    Any stderr generated by the git process will be piped to error_stream,
    which must have a file descriptor.

    Return (cgi_header, response_body_generator). The cgi_header is the
    string of raw headers returned by git ending with just one CRLF. The
    response sent back to the client will need an additional blank line
    separating this from the response body.

    Raise EnvironmentError (errno 1) if a CGI/HTTP header is not returned
    from git http-backend."""
    input_length = int(cgi_environ.get('CONTENT_LENGTH', '') or 0)
    proc = subprocess.Popen(
        ['/opt/local/bin/git', 'http-backend'],
        bufsize=DEFAULT_CHUNK_SIZE,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=error_stream,
        env=cgi_environ
    )
    cgi_header, response_body_generator = _communicate_with_git(
        proc, input_stream, input_length
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

    The git repo (my-repo.git) is located at GIT_PROJECT_ROOT + PATH_INFO
    (if GIT_PROJECT_ROOT is defined) or at PATH_TRANSLATED.

    If REMOTE_USER is set in wsgi_environ, you should normally leave user
    alone.
    """
    cgi_environ = dict(wsgi_environ)
    for key, value in cgi_environ.items():  # NOT iteritems, due to "del"
        if not isinstance(value, str):
            del cgi_environ[key]
    cgi_environ['GIT_HTTP_EXPORT_ALL'] = '1'
    cgi_environ['GIT_PROJECT_ROOT'] = git_project_root
    if user:
        cgi_environ['REMOTE_USER'] = user
    cgi_environ.setdefault('REMOTE_USER', 'unknown')
    return cgi_environ


def parse_cgi_header(cgi_header):
    """Given the raw header returned by the CGI, return
    (status_line, list_of_headers). This adapts the CGI header
    to WSGI conventions."""
    header_dict = {}
    names = []  # to preserve order
    raw_lines = cgi_header.split(CRLF)
    assert raw_lines[-1] == ''
    for raw_line in raw_lines[:-1]:
        name, padded_value = raw_line.strip().split(':', 1)
        value = padded_value.strip()
        header_dict[name] = value
        if name != 'Status':
            names.append(name)
    status_line = header_dict.pop('Status', None) or '200 OK'
    list_of_headers = [(name, header_dict[name]) for name in names]
    return status_line, list_of_headers


def _communicate_with_git(proc, input_stream, input_length):
    # Given a subprocess.Popen object:
    # * Start writing request data
    # * Start reading stdout and possibly stderr
    # * Extract the cgi_header
    # * Construct a generator for everything that comes after the header
    # * Return (cgi_header, response_body_generator)
    # (The generator is responsible for extracting all data and cleaning up.)
    # Raise EnvironmentError (errno 1) if header is not returned from proc.
    threading.Thread(target=_input_data_pump,
                     args=(proc, input_stream, input_length)).start()
    chunks = ['']  # Dummy str at start helps here.
    header_end = None
    while not header_end:
        total_bytes_read = sum(map(len, chunks))
        if total_bytes_read > DEFAULT_MAX_HEADER_SIZE:
            raise EnvironmentError(
                1,
                'Read %d bytes from "git http-backend" without '
                'finding header boundary.' % total_bytes_read,
            )  # TODO: Test this.
        chuck_data = proc.stdout.read(DEFAULT_CHUNK_SIZE)
        if not chuck_data:
            raise EnvironmentError(
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


def _input_data_pump(proc, input_stream, input_length):
    # Thread for feeding input to git
    # TODO: Currently using threads due to lack of universal standard for
    # async event loops in web applications.
    bytes_read = 0
    while bytes_read < input_length:
        bytes_to_read = min(DEFAULT_CHUNK_SIZE, input_length - bytes_read)
        current_data = input_stream.read(bytes_to_read)
        bytes_read += len(current_data)
        proc.stdin.write(current_data)
    proc.stdin.close()


def _find_header_end_in_2_chunks(chunk0, chunk1):
    # Search for the 4-byte HEADER_END in either the end of the first chunk
    # (with the 4-byte boundary stretching into the second chunk) or within
    # the second chunk starting at 0.
    # Return as (header_end_on_boundary, index_within_chunk).
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
    return data_str.find(HEADER_END)


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
    header_chunks.append(CRLF)  # Line end might have been split.
    header = ''.join(header_chunks)
    remainder = chunks[-1][body_start_index:]
    return header, remainder


def _response_body_generator(remainder, proc):
    # The generator returned up the stack to the WSGI application.
    # Yields chunks of data from the subprocess output.
    yield remainder
    current_data = proc.stdout.read(DEFAULT_CHUNK_SIZE)
    while current_data:
        yield current_data
        current_data = proc.stdout.read(DEFAULT_CHUNK_SIZE)
    # TODO: Do we need this?
    while proc.poll() is None:
        yield ''
