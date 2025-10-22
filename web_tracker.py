"""
Real-time Web-Based Vessel Tracker with Map Visualization
Displays tracked vessels on an interactive map with live updates.
"""

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
import websocket
import json
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime

# Configuration
DB_NAME = "vessel_static_data.db"
API_KEY_FILE = "api.txt"
WEBSOCKET_URL = "wss://stream.aisstream.io/v0/stream"
MAX_MMSI_PER_CONNECTION = 50

# Flask app
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = 'ais-tracker-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Global state
API_KEY = None
vessel_positions = {}  # {mmsi: {lat, lon, sog, cog, timestamp, name, ...}}
vessel_static_data = {}  # {mmsi: {name, length, type, flag, ...}}
tracking_active = False


def load_api_key():
    """Load the API key from api.txt file."""
    try:
        script_dir = Path(__file__).parent
        api_file_path = script_dir / API_KEY_FILE
        
        with open(api_file_path, 'r') as f:
            lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if line:
                    return line
        raise ValueError("No API key found in api.txt")
    except Exception as e:
        raise Exception(f"Error loading API key: {e}")


def get_filtered_vessels():
    """Get vessels from database matching filter criteria."""
    script_dir = Path(__file__).parent
    db_path = script_dir / DB_NAME
    
    conn = sqlite3.connect(db_path, timeout=30)
    cursor = conn.cursor()
    
    query = '''
        SELECT mmsi, name, ship_type, length, beam, imo, call_sign, flag_state
        FROM vessels_static
        WHERE length >= 100
          AND mmsi IS NOT NULL
          AND length IS NOT NULL
          AND (ship_type IS NULL OR ship_type NOT IN (71, 72))
        ORDER BY length DESC
    '''
    
    cursor.execute(query)
    vessels = cursor.fetchall()
    conn.close()
    
    # Store static data
    for vessel in vessels:
        mmsi, name, ship_type, length, beam, imo, call_sign, flag_state = vessel
        vessel_static_data[mmsi] = {
            'name': name or 'Unknown',
            'ship_type': ship_type,
            'length': length,
            'beam': beam,
            'imo': imo,
            'call_sign': call_sign,
            'flag_state': flag_state or 'Unknown'
        }
    
    return [vessel[0] for vessel in vessels]


class VesselTrackerWebSocket:
    """Handles WebSocket connection for tracking vessels."""
    
    def __init__(self, batch_id, mmsi_batch, api_key):
        self.batch_id = batch_id
        self.mmsi_batch = mmsi_batch
        self.api_key = api_key
        self.ws_app = None
        self.thread = None
        self.running = False
        
    def on_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
            
            if "error" in data or "Error" in data:
                print(f"[Batch {self.batch_id}] ERROR: {data}")
                return
            
            msg_type = data.get("MessageType")
            
            if msg_type == "PositionReport":
                metadata = data.get("MetaData", {})
                position_data = data.get("Message", {}).get("PositionReport", {})
                
                mmsi = metadata.get("MMSI")
                lat = metadata.get("latitude")
                lon = metadata.get("longitude")
                sog = position_data.get("Sog", 0)
                cog = position_data.get("Cog", 0)
                timestamp = metadata.get("time_utc", datetime.utcnow().isoformat())
                
                if mmsi and lat and lon:
                    # Update vessel position
                    vessel_positions[mmsi] = {
                        'lat': lat,
                        'lon': lon,
                        'sog': sog,
                        'cog': cog,
                        'timestamp': timestamp,
                        'name': vessel_static_data.get(mmsi, {}).get('name', 'Unknown'),
                        'length': vessel_static_data.get(mmsi, {}).get('length'),
                        'flag_state': vessel_static_data.get(mmsi, {}).get('flag_state', 'Unknown')
                    }
                    
                    # Emit to all connected web clients
                    socketio.emit('vessel_update', {
                        'mmsi': mmsi,
                        'position': vessel_positions[mmsi]
                    })
                    
                    print(f"[Position] {mmsi} - {vessel_positions[mmsi]['name']}: {lat:.4f}, {lon:.4f}")
                
        except Exception as e:
            print(f"[Batch {self.batch_id}] Error: {e}")
    
    def on_error(self, ws, error):
        """Handle WebSocket errors."""
        error_str = str(error)
        if "Connection to remote host was lost" not in error_str:
            print(f"[Batch {self.batch_id}] Error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close."""
        print(f"[Batch {self.batch_id}] Connection closed")
    
    def on_open(self, ws):
        """Handle WebSocket open and send subscription."""
        print(f"[Batch {self.batch_id}] Connected - Tracking {len(self.mmsi_batch)} vessels")
        
        mmsi_strings = [str(mmsi) for mmsi in self.mmsi_batch]
        
        subscribe_message = {
            "APIKey": self.api_key,
            "FiltersShipMMSI": mmsi_strings,
            "BoundingBoxes": [[[90, -180], [-90, 180]]]
        }
        
        ws.send(json.dumps(subscribe_message))
        print(f"[Batch {self.batch_id}] Subscription sent")
    
    def start(self):
        """Start the WebSocket connection."""
        self.running = True
        self.thread = threading.Thread(target=self._run_forever, daemon=True)
        self.thread.start()
    
    def _run_forever(self):
        """Run WebSocket with auto-reconnect."""
        while self.running:
            try:
                self.ws_app = websocket.WebSocketApp(
                    WEBSOCKET_URL,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close
                )
                self.ws_app.run_forever()
                
                if self.running:
                    time.sleep(5)
                    
            except Exception as e:
                print(f"[Batch {self.batch_id}] Exception: {e}")
                time.sleep(5)


# Flask routes
@app.route('/')
def index():
    """Serve the main map page."""
    return render_template('map.html')


@app.route('/api/vessels')
def get_vessels():
    """Get all tracked vessels and their current positions."""
    vessels = []
    for mmsi, static in vessel_static_data.items():
        vessel_info = {
            'mmsi': mmsi,
            'name': static['name'],
            'length': static['length'],
            'flag_state': static['flag_state'],
            'ship_type': static['ship_type']
        }
        
        # Add position if available
        if mmsi in vessel_positions:
            vessel_info.update(vessel_positions[mmsi])
        
        vessels.append(vessel_info)
    
    return jsonify(vessels)


@app.route('/api/stats')
def get_stats():
    """Get tracking statistics."""
    return jsonify({
        'total_vessels': len(vessel_static_data),
        'vessels_with_position': len(vessel_positions),
        'tracking_active': tracking_active
    })


@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    print('Client connected')
    emit('initial_data', {
        'vessels': list(vessel_static_data.keys()),
        'positions': vessel_positions
    })


def start_tracking():
    """Start tracking vessels via WebSocket."""
    global tracking_active, API_KEY
    
    try:
        print("Loading API key...")
        API_KEY = load_api_key()
        
        print("Loading vessels from database...")
        mmsi_list = get_filtered_vessels()
        print(f"Loaded {len(mmsi_list)} vessels for tracking")
        
        # Create batches
        batches = []
        for i in range(0, len(mmsi_list), MAX_MMSI_PER_CONNECTION):
            batches.append(mmsi_list[i:i + MAX_MMSI_PER_CONNECTION])
        
        print(f"Creating {len(batches)} tracking connections...")
        
        # Start trackers
        for i, batch in enumerate(batches, 1):
            tracker = VesselTrackerWebSocket(i, batch, API_KEY)
            tracker.start()
            time.sleep(1)
        
        tracking_active = True
        print(f"Tracking {len(mmsi_list)} vessels across {len(batches)} connections")
        
    except Exception as e:
        print(f"Error starting tracking: {e}")


if __name__ == '__main__':
    # Start tracking in background
    tracking_thread = threading.Thread(target=start_tracking, daemon=True)
    tracking_thread.start()
    
    # Give tracking a moment to initialize
    time.sleep(2)
    
    print("\n" + "="*70)
    print("VESSEL TRACKER WEB INTERFACE")
    print("="*70)
    print("Open your browser and go to:")
    print("  http://localhost:5000")
    print("="*70 + "\n")
    
    # Start Flask server
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
