from flask import Flask

from db import init_db
from routes import main_bp
import config

app = Flask(__name__)
app.secret_key = 'hex_grid_secret_key'
app.register_blueprint(main_bp, url_prefix='/gerrymandering')


def _parse_args_and_run():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", type=int, help="port number")
    parser.add_argument("-g", "--groups", type=int, help="number of attending groups to enable")
    args = parser.parse_args()
    port_number = args.port if args.port is not None else 18303
    if args.groups is not None:
        if args.groups < 0:
            raise ValueError("Error: Number of groups must be non-negative.")
        if args.groups > len(config.USER_REGISTRY):
            raise ValueError("Error: Too many groups.")
        try:
            config.GROUP_COUNT = int(args.groups)
        except Exception:
            pass # default: GROUP_COUNT = 10

    init_db()
    app.run(host='0.0.0.0', port=port_number, debug=True)


if __name__ == '__main__':
    _parse_args_and_run()
