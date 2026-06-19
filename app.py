from flask import Flask
import argparse

from db import init_db
from routes import main_bp

parser = argparse.ArgumentParser()
parser.add_argument("-p", "--port", type=int, help="port number")
args = parser.parse_args()
port_number = args.port if args.port is not None else 18303

app = Flask(__name__)
app.secret_key = 'hex_grid_secret_key'
app.register_blueprint(main_bp)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=port_number, debug=True)
