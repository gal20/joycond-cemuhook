import sys
import evdev
from threading import Thread
import socket
import struct
from binascii import crc32
import time
import asyncio
import dbus
import json
import argparse
import subprocess
from termcolor import colored

def print_verbose(str):
    global args
    if args.verbose:
        print(colored("Debug: ", "red", attrs=["bold"])+str)

def clamp(my_value, min_value, max_value):
    return max(min(my_value, max_value), min_value)

def abs_to_button(value):
    if value > 0.75:
        value = 255
    else:
        value = 0
    return value

class Message(list):
    Types = dict(version=bytes([0x00, 0x00, 0x10, 0x00]),
                 ports=bytes([0x01, 0x00, 0x10, 0x00]),
                 data=bytes([0x02, 0x00, 0x10, 0x00]))

    def __init__(self, message_type, data):
        self.extend([
            0x44, 0x53, 0x55, 0x53,  # DSUS,
            0xE9, 0x03,  # protocol version (1001),
        ])

        # data length
        self.extend(bytes(struct.pack('<H', len(data) + 4)))

        self.extend([
            0x00, 0x00, 0x00, 0x00,  # place for CRC32
            0xff, 0xff, 0xff, 0xff,  # server ID
        ])

        self.extend(Message.Types[message_type])  # data type

        self.extend(data)

        # CRC32
        crc = crc32(bytes(self)) & 0xffffffff
        self[8:12] = bytes(struct.pack('<I', crc))

class SwitchDevice:
    def __init__(self, server, device, motion_device):
        self.server = server

        self.device = device
        self.motion_device = motion_device

        self.disconnected = False

        self.name = device.name
        self.serial = motion_device.uniq if motion_device.uniq != "" else "00:00:00:00:00:00"
        self.mac = [int("0x"+part, 16) for part in self.serial.split(":")]

        self.device_capabilities = device.capabilities(absinfo=True)

        self.state = {
            "left_analog_x": 0x00,
            "left_analog_y": 0x00,
            "right_analog_x": 0x00,
            "right_analog_y": 0x00,
            "dpad_up": 0x00,
            "dpad_down": 0x00,
            "dpad_left": 0x00,
            "dpad_right": 0x00,
            "button_cross": 0x00,
            "button_circle": 0x00,
            "button_square": 0x00,
            "button_triangle": 0x00,
            "button_l1": 0x00,
            "button_l2": 0x00,
            "button_l3": 0x00,
            "button_r1": 0x00,
            "button_r2": 0x00,
            "button_r3": 0x00,
            "button_share": 0x00,
            "button_options": 0x00,
            "button_ps": 0x00,
            "motion_y": 0x00,
            "motion_x": 0x00,
            "motion_z": 0x00,
            "orientation_roll": 0x00,
            "orientation_yaw": 0x00,
            "orientation_pitch": 0x00,
            "timestamp": 0x00,
            "battery": 0x00
        }

        self.keymap = None

        with open('profiles/'+self.name+'.json', 'r') as f:
            self.keymap = json.load(f)

        self.motion_x = 0
        self.motion_y = 0
        self.motion_z = 0
        
        self.accel_x = 0
        self.accel_y = 0
        self.accel_z = 0

        # Input reading thread
        self.event_thread = Thread(target=self.handle_events)
        self.event_thread.daemon = True
        self.event_thread.start()

        # Motion reading thread
        self.motion_event_thread = Thread(target=self.handle_motion_events)
        self.motion_event_thread.daemon = True
        self.motion_event_thread.start()

        # Battery level reading thread
        self.battery_level = None
        self.battery_state = None
        self.dbus_interface = None
        self.dbus_properties_interface = None
        self.get_battery_dbus_interface()

        if self.dbus_interface and self.dbus_properties_interface:
            self.battery_thread = Thread(target=self.get_battery_level)
            self.battery_thread.daemon = True
            self.battery_thread.start()
    
    def handle_motion_events(self):
        print_verbose("Motion events thread started")
        if self.motion_device:
            try:
                asyncio.set_event_loop(asyncio.new_event_loop())
                for event in self.motion_device.read_loop():
                    if event.type == evdev.ecodes.SYN_REPORT:
                        self.server.report(self, True)
                        self.motion_x = 0
                        self.motion_y = 0
                        self.motion_z = 0
                    elif event.type == evdev.ecodes.EV_ABS:
                        # Get info about the axis we're reading the event from
                        axis = self.motion_device.absinfo(event.code)

                        if event.code == evdev.ecodes.ABS_RX:
                            self.motion_x += event.value / axis.resolution
                        if event.code == evdev.ecodes.ABS_RY:
                            self.motion_y += event.value / axis.resolution
                        if event.code == evdev.ecodes.ABS_RZ:
                            self.motion_z += event.value / axis.resolution
                        if event.code == evdev.ecodes.ABS_X:
                            self.accel_x = event.value / axis.resolution
                        if event.code == evdev.ecodes.ABS_Y:
                            self.accel_y = event.value / axis.resolution
                        if event.code == evdev.ecodes.ABS_Z:
                            self.accel_z = event.value / axis.resolution
            except(OSError, RuntimeError) as e:
                print("Device motion disconnected: " + self.name)
                asyncio.get_event_loop().close()
    
    def handle_events(self):
        print_verbose("Input events thread started")
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())

            for event in self.device.read_loop():
                if event.type == evdev.ecodes.SYN_REPORT:
                    self.server.report(self)
                if event.type == evdev.ecodes.EV_ABS:
                    for ps_key in self.keymap:
                        key_mine = self.keymap.get(ps_key, None)
                        if key_mine == None:
                            continue

                        if event.code == evdev.ecodes.ecodes.get(key_mine.replace("-", ""), None):
                            axis = self.device.absinfo(evdev.ecodes.ecodes.get(key_mine.replace("-", "")))
                            self.state[ps_key] = event.value / axis.max
                            self.state[ps_key] = clamp(self.state[ps_key], -1, 1)
                            if(key_mine[0] == "-"):
                                self.state[ps_key] = -self.state[ps_key]

                if event.type == evdev.ecodes.EV_KEY:
                    for ps_key in self.keymap:
                        if event.code == evdev.ecodes.ecodes.get(self.keymap.get(ps_key, None), None):
                            self.state[ps_key] = 0xFF if event.value == 1 else 0x00

        except(OSError, RuntimeError) as e:
            print("Device disconnected: " + self.name)
            self.server.report_clean(self)
            self.disconnected = True
            asyncio.get_event_loop().close()
    
    def get_battery_dbus_interface(self):
        bus = dbus.SystemBus()
        upower = bus.get_object('org.freedesktop.UPower', '/org/freedesktop/UPower')

        upower_list = upower.get_dbus_method('EnumerateDevices', 'org.freedesktop.UPower')()

        for device in upower_list:
            dev = bus.get_object('org.freedesktop.UPower', device)

            dbus_interface = dbus.Interface(dev, 'org.freedesktop.UPower.Device')
            dbus_interface.Refresh()

            dbus_properties_interface = dbus.Interface(dev, 'org.freedesktop.DBus.Properties')
            properties = dbus_properties_interface.GetAll("org.freedesktop.UPower.Device")

            if properties["Serial"] == self.serial:
                self.dbus_interface = dbus_interface
                self.dbus_properties_interface = dbus_properties_interface
                self.battery_level = properties["Percentage"]
                self.battery_state = properties["State"]
                print_verbose("Found dbus interface for battery level reading. Value: "+str(self.battery_level))
                return True
        return False
    
    def get_battery_level(self):
        try:
            print_verbose("Battery level reading thread started")
            while(self.dbus_interface != None):
                self.dbus_interface.Refresh()
                properties = self.dbus_properties_interface.GetAll("org.freedesktop.UPower.Device")
                if properties["Percentage"] != self.battery_level:
                    print_verbose("Battery level changed")
                    self.battery_level = properties["Percentage"]
                    self.battery_state = properties["State"]
                    self.server.print_slots()
                time.sleep(30)
        except Exception as e:
            print(e)
            self.battery_level = None
            self.battery_state = None
    
    def get_report(self):
        report = self.state

        if self.battery_level == None:
            report["battery"] = 0x00
        elif self.battery_level < 10:
            report["battery"] = 0x01
        elif self.battery_level < 25:
            report["battery"] = 0x02
        elif self.battery_level < 75:
            report["battery"] = 0x03
        elif self.battery_level < 90:
            report["battery"] = 0x04
        else:
            report["battery"] = 0x05
        
        self.state["battery"] = report["battery"]

        if self.device == None:
            return report
        
        return report

class UDPServer:
    def __init__(self, host='', port=26760):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        print_verbose("Started UDP server with ip "+str(host)+", port "+str(port))
        self.counter = 0
        self.clients = dict()
        self.slots = [None, None, None, None]
        self.locks = [asyncio.Lock(), asyncio.Lock(), asyncio.Lock(), asyncio.Lock()]
        self.lock = asyncio.Lock()

    def _res_ports(self, index):
        data = [
            index,  # pad id
            0x00,  # state (disconnected)
            0x03,  # model (generic)
            0x01,  # connection type (usb)
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, # Mac
            0x00,  # battery (charged)
            0x00,  # ?
        ]

        if self.slots[index]:
            device = self.slots[index]
            data = [
                index,  # pad id
                0x02,  # state (connected)
                0x03,  # model (generic)
                0x01,  # connection type (usb)
                device.mac[0], device.mac[1], device.mac[2],  # MAC1
                device.mac[3], device.mac[4], device.mac[5],  # MAC2
                device.state["battery"],  # battery (charged)
                0x00,  # ?
            ]

        return Message('ports', data)
    
    @staticmethod
    def _compat_ord(value):
        return ord(value) if sys.version_info < (3, 0) else value

    def _req_ports(self, message, address):
        requests_count = struct.unpack("<i", message[20:24])[0]
        for i in range(requests_count):
            index = self._compat_ord(message[24 + i])
            self.sock.sendto(bytes(self._res_ports(index)), address)

    def _req_data(self, message, address):
        flags = self._compat_ord(message[24])
        reg_id = self._compat_ord(message[25])
        slot_id = self._compat_ord(message[21])
        # reg_mac = message[26:32]

        if flags == 0 and reg_id == 0:  # TODO: Check MAC
            if address not in self.clients:
                print('[udp] Client connected: {0[0]}:{0[1]}'.format(address))

                self.clients[address] = {
                    "timestamp": time.time(),
                    "controllers": [0,0,0,0]
                }
            else:
                self.clients[address]["timestamp"] = time.time()
                self.clients[address]["controllers"][slot_id] = 1
    
    def _res_data(self, controller_index, message):
        now = time.time()
        for address, data in self.clients.copy().items():
            if data["controllers"][controller_index] == 1:
                if now - data["timestamp"] < 5:
                    self.sock.sendto(message, address)
                else:
                    print('[udp] Client disconnected: {0[0]}:{0[1]}'.format(address))
                    del self.clients[address]
    
    def _handle_request(self, request):
        message, address = request

        # client_id = message[12:16]
        msg_type = message[16:20]

        if msg_type == Message.Types['version']:
            return
        elif msg_type == Message.Types['ports']:
            self._req_ports(message, address)
        elif msg_type == Message.Types['data']:
            self._req_data(message, address)
        else:
            print('[udp] Unknown message type: ' + str(msg_type))

    def report(self, device, report_motion=False):
        if device == None:
            return None
        
        if device.device == None:
            return None
        
        i = self.slots.index(device) if device in self.slots else -1

        if i == -1:
            return None
        
        device_state = device.get_report()

        data = [
            i & 0xff,  # pad id
            0x02 if device.device != None else 0x00,  # state (connected)
            0x02,  # model (generic)
            0x02,  # connection type (usb)
            device.mac[0], device.mac[1], device.mac[2],  # MAC1
            device.mac[3], device.mac[4], device.mac[5],  # MAC2
            device_state["battery"],  # battery (charged)
            0x01  # is active (true)
        ]

        data.extend(struct.pack('<I', self.counter))
        self.counter += 1

        buttons1 = 0x00
        buttons1 |= int(abs_to_button(device_state.get("button_share", 0x00))/255)
        buttons1 |= int(abs_to_button(device_state.get("button_l3", 0x00))/255) << 1
        buttons1 |= int(abs_to_button(device_state.get("button_r3", 0x00))/255) << 2
        buttons1 |= int(abs_to_button(device_state.get("button_options", 0x00))/255) << 3
        buttons1 |= int(abs_to_button(device_state.get("dpad_up", 0x00))/255) << 4
        buttons1 |= int(abs_to_button(device_state.get("dpad_right", 0x00))/255) << 5
        buttons1 |= int(abs_to_button(device_state.get("dpad_down", 0x00))/255) << 6
        buttons1 |= int(abs_to_button(device_state.get("dpad_left", 0x00))/255) << 7

        buttons2 = 0x00
        buttons2 |= int(abs_to_button(device_state.get("button_l2", 0x00))/255)
        buttons2 |= int(abs_to_button(device_state.get("button_r2", 0x00))/255) << 1
        buttons2 |= int(abs_to_button(device_state.get("button_l1", 0x00))/255) << 2
        buttons2 |= int(abs_to_button(device_state.get("button_r1", 0x00))/255) << 3
        buttons2 |= int(abs_to_button(device_state.get("button_triangle", 0x00))/255) << 4
        buttons2 |= int(abs_to_button(device_state.get("button_circle", 0x00))/255) << 5
        buttons2 |= int(abs_to_button(device_state.get("button_cross", 0x00))/255) << 6
        buttons2 |= int(abs_to_button(device_state.get("button_square", 0x00))/255) << 7

        data.extend([
            buttons1,
            buttons2,
            abs_to_button(device_state.get("button_ps", 0x00)),  # PS
            0x00,  # Touch

            int(device_state.get("left_analog_x", 0x00) * 127) + 128,  # position left x
            int(device_state.get("left_analog_y", 0x00) * 127) + 128,  # position left y
            int(device_state.get("right_analog_x", 0x00) * 127) + 128,  # position right x
            int(device_state.get("right_analog_y", 0x00) * 127) + 128,  # position right y

            abs_to_button(device_state.get("dpad_left", 0x00)),  # dpad left
            abs_to_button(device_state.get("dpad_down", 0x00)),  # dpad down
            abs_to_button(device_state.get("dpad_right", 0x00)),  # dpad right
            abs_to_button(device_state.get("dpad_up", 0x00)),  # dpad up

            abs_to_button(device_state.get("button_square", 0x00)),  # square
            abs_to_button(device_state.get("button_cross", 0x00)),  # cross
            abs_to_button(device_state.get("button_circle", 0x00)),  # circle
            abs_to_button(device_state.get("button_triangle", 0x00)),  # triange

            abs_to_button(device_state.get("button_r1", 0x00)),  # r1
            abs_to_button(device_state.get("button_l1", 0x00)),  # l1

            abs_to_button(device_state.get("button_r2", 0x00)),  # r2
            abs_to_button(device_state.get("button_l2", 0x00)),  # l2

            0x00,  # track pad first is active (false)
            0x00,  # track pad first id

            0x00, 0x00,  # trackpad first x
            0x00, 0x00,  # trackpad first y

            0x00,  # track pad second is active (false)
            0x00,  # track pad second id

            0x00, 0x00,  # trackpad second x
            0x00, 0x00,  # trackpad second y
        ])

        data.extend(bytes(struct.pack('<Q', int(time.time() * 10**6))))

        if device.motion_device != None:
            sensors = [
                # Acceleration in g's
                device.accel_y,
                - device.accel_z,
                device.accel_x,
                # Gyro rotation in deg/s
                - device.motion_y,
                - device.motion_z,
                device.motion_x,
            ]

            if report_motion == False:
                sensors[3] = 0
                sensors[4] = 0
                sensors[5] = 0
        else:
            sensors = [0, 0, 0, 0, 0, 0]

        for sensor in sensors:
            data.extend(struct.pack('<f', float(sensor)))
        
        self._res_data(i, bytes(Message('data', data)))
    
    def report_clean(self, device):
        i = self.slots.index(device) if device in self.slots else -1

        data = [
            i & 0xff,  # pad id
            0x00,  # state (disconnected)
            0x03,  # model (generic)
            0x01,  # connection type (usb)
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, # Mac
            0x00,  # battery (charged)
            0x00,  # ?
        ]

        self._res_data(i, bytes(Message('data', data)))
    
    def handle_devices(self):
        asyncio.set_event_loop(asyncio.new_event_loop())

        print("Looking for Nintendo Switch controllers...")
        
        while True:
            try:
                evdev_devices = [evdev.InputDevice(path) for path in evdev.list_devices()]

                for d in evdev_devices:
                    if d.name == "Nintendo Switch Left Joy-Con" or \
                    d.name == "Nintendo Switch Right Joy-Con" or \
                    d.name == "Nintendo Switch Pro Controller" or \
                    d.name == "Nintendo Switch Combined Joy-Cons":
                        found = any(my_device.device == d for my_device in self.slots if my_device != None)

                        if not found:
                            print("Found ["+d.name+"] - mac: "+d.uniq)

                            motion_d = None

                            for dd in evdev_devices: # try to automagically identify correct IMU for individual Joy-Cons and Pro Controller
                                if dd.uniq == d.uniq and dd != d and dd.uniq != "": # combined Joy-Cons have blank uniqs and should not be assigned to any random evdev device
                                    motion_d = dd
                                    break
                            
                            if motion_d == None:
                                print("Select motion provider for ["+d.name+"]: ")
                                for i, dd in enumerate(evdev_devices):
                                    print(
                                        ("*" if "Nintendo" in dd.name and "IMU" in dd.name else " ") + 
                                        str(i) + " " + dd.name + " - mac: " + dd.uniq
                                    )
                                motion_d = evdev_devices[int(input(""))]
                            
                            if motion_d:
                                print("Using [" + motion_d.name + "] as motion provider for [" + d.name + "]")
                            else:
                                print("Not using motion inputs for [" + d.name + "]")

                            for i in range(4):
                                if self.slots[i] == None:
                                    self.slots[i] = SwitchDevice(self, d, motion_d)
                                    break
                            
                            self.print_slots()
                
                for i in range(4):
                    if self.slots[i] != None and self.slots[i].disconnected == True:
                        self.slots[i] = None
                        self.print_slots()
                
                time.sleep(0.2) # sleep for 0.2 seconds to avoid 100% cpu usage
            except Exception as e:
                print(e)
                    
    
    def print_slots(self):
        print(colored("======================== Slots ========================", attrs=["bold"]))
        
        print (colored("{:<14} {:<12} {:<12} {:<12}", attrs=["bold"])
            .format("Device", "Battery Lv", "Motion Dev", "MAC Addr"))

        for i in range(4):
            if self.slots[i] == None:
                print(str(i+1)+" ❎ ")
            else:
                device = str(i+1)+" "
                if "Left" in self.slots[i].name:
                    device += "🕹️ L"
                elif "Right" in self.slots[i].name:
                    device += "🕹️ R"
                elif "Combined" in self.slots[i].name:
                    device += "🎮 L+R"
                else:
                    device += "🎮 Pro"
                
                battery = ""
                if self.slots[i].battery_level:
                    battery += str(self.slots[i].battery_level)
                    battery += " "
                    battery += chr(ord('▁')-1 + int(self.slots[i].battery_level/10))
                else:
                    battery += "❌"

                motion_d = ""
                if self.slots[i].motion_device:
                    motion_d += "✔️"
                else:
                    motion_d += "❌"
                
                mac = self.slots[i].serial

                print(("{:<14} "+colored("{:<12}", "green")+" {:<12} {:<12}")
                    .format(device, battery, motion_d, mac))

        print(colored("=======================================================", attrs=["bold"]))

    def _worker(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        while True:
            self._handle_request(self.sock.recvfrom(1024))

    def start(self):
        self.thread = Thread(target=self._worker)
        self.thread.daemon = True
        self.thread.start()

        self.device_thread = Thread(target=self.handle_devices)
        self.device_thread.daemon = True
        self.device_thread.start()

        self.device_thread.join()


parser = argparse.ArgumentParser()
parser.add_argument("-v", "--verbose", help="show debug messages", action="store_true")
parser.add_argument("-ip", "--ip", help="set custom port, default is 127.0.0.1")
parser.add_argument("-p", "--port", help="set custom port, default is 26760")
args = parser.parse_args()

# Check if hid_nintendo module is installed
process = subprocess.Popen(["modinfo", "hid_nintendo"], stdout=subprocess.DEVNULL)
process.communicate()
hid_nintendo_installed = process.returncode

if hid_nintendo_installed == 1:
    print("Seems like hid_nintendo is not installed.")
    exit()

# Check if hid_nintendo module is loaded
process = subprocess.Popen(["/bin/sh", "-c", 'lsmod | grep hid_nintendo'], stdout=subprocess.DEVNULL)
process.communicate()
hid_nintendo_loaded = process.returncode

if hid_nintendo_loaded == 1:
    print("Seems like hid_nintendo is not loaded. Load it with 'sudo modprobe hid_nintendo'.")
    exit()

ip = '127.0.0.1'
if args.ip:
    ip = args.ip

port = 26760
if args.port:
    port = int(args.port)

server = UDPServer(ip, port)
server.start()