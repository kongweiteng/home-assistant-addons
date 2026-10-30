"""Fixed options and Unix-only composition; explicit flags default disabled."""
import sys

from .api import create_server
from .contract import ObserverError
from .settings import compose,load_settings


def main():
    try:
        service=compose(load_settings())
        # This release provides artifact observation only. The optional API
        # hook has no factory, registration reader or Controller dependency.
        server=create_server('0.0.0.0',8099,service=service)
    except ObserverError as error:
        print(error.code,file=sys.stderr)
        return 2
    except Exception:
        # OS/configuration errors can include private paths and values.
        print('observer_startup_unavailable',file=sys.stderr)
        return 2
    server.serve_forever()
    return 0


if __name__=='__main__':raise SystemExit(main())
