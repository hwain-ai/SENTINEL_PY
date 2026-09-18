"""Run pinned mutmut without its automatic wall-clock or CPU deadlines.

Only the child backend process is changed; the installed dependency is untouched.
Cancellation is owned by SENTINEL's process-group cleanup.
"""

import resource
import mutmut.__main__ as backend
from child_lifetime import bind_forked_children


def main():
    bind_forked_children()
    original = resource.setrlimit

    def set_limit(kind, limits):
        if kind != resource.RLIMIT_CPU:
            original(kind, limits)

    backend.resource.setrlimit = set_limit
    backend.register_timeout = lambda **kwargs: None
    backend.cli()


if __name__ == "__main__":
    main()
