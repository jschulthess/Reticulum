# Reticulum License
#
# Copyright (c) 2016-2025 Mark Qvist
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# - The Software shall not be used in any kind of system which includes amongst
#   its functions the ability to purposefully do harm to human beings.
#
# - The Software shall not be used, directly or indirectly, in the creation of
#   an artificial intelligence, machine learning or language model training
#   dataset, including but not limited to any use that contributes to the
#   training or development of such a model or algorithm.
#
# - The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from RNS.Interfaces.Interface import Interface
from time import sleep
import sys
import threading
import time
import RNS

class KISS():
    FEND              = 0xC0
    FESC              = 0xDB
    TFEND             = 0xDC
    TFESC             = 0xDD
    CMD_UNKNOWN       = 0xFE
    CMD_DATA          = 0x00
    CMD_TXDELAY       = 0x01
    CMD_P             = 0x02
    CMD_SLOTTIME      = 0x03
    CMD_TXTAIL        = 0x04
    CMD_FULLDUPLEX    = 0x05
    CMD_SETHARDWARE   = 0x06
    CMD_READY         = 0x0F
    CMD_RETURN        = 0xFF

    @staticmethod
    def escape(data):
        data = data.replace(bytes([0xdb]), bytes([0xdb, 0xdd]))
        data = data.replace(bytes([0xc0]), bytes([0xdb, 0xdc]))
        return data

class AX25():
    PID_NOLAYER3    = 0xF0
    CTRL_UI         = 0x03
    CRC_CORRECT     = bytes([0xF0])+bytes([0xB8])
    HEADER_SIZE     = 16


class AX25KISSInterface(Interface):
    MAX_CHUNK = 32768
    BITRATE_GUESS = 1200
    DEFAULT_IFAC_SIZE = 8

    owner    = None
    port     = None
    speed    = None
    databits = None
    parity   = None
    stopbits = None
    serial   = None

    def __init__(self, owner, configuration):
        import importlib.util
        if importlib.util.find_spec('serial') != None:
            import serial
        else:
            RNS.log("Using the AX.25 KISS interface requires a serial communication module to be installed.", RNS.LOG_CRITICAL)
            RNS.log("You can install one with the command: python3 -m pip install pyserial", RNS.LOG_CRITICAL)
            RNS.panic()

        super().__init__()

        c = Interface.get_config_obj(configuration)
        name = c["name"]
        preamble = int(c["preamble"]) if "preamble" in c else None
        txtail = int(c["txtail"]) if "txtail" in c else None
        persistence = int(c["persistence"]) if "persistence" in c else None
        slottime = int(c["slottime"]) if "slottime" in c else None
        flow_control = c.as_bool("flow_control") if "flow_control" in c else False
        port = c["port"] if "port" in c else None
        speed = int(c["speed"]) if "speed" in c else 9600
        databits = int(c["databits"]) if "databits" in c else 8
        parity = c["parity"] if "parity" in c else "N"
        stopbits = int(c["stopbits"]) if "stopbits" in c else 1

        callsign = c["callsign"] if "callsign" in c else ""
        ssid = int(c["ssid"]) if "ssid" in c else -1

        if port == None:
            raise ValueError("No port specified for serial interface")

        self.HW_MTU = 564
        
        self.pyserial = serial
        self.serial   = None
        self.owner    = owner
        self.name     = name
        self.src_call = callsign.upper().encode("ascii")
        self.src_ssid = ssid
        self.dst_call = "APZRNS".encode("ascii")
        self.dst_ssid = 0
        self.port     = port
        self.speed    = speed
        self.databits = databits
        self.parity   = serial.PARITY_NONE
        self.stopbits = stopbits
        self.timeout  = 100
        self.online   = False
        self.bitrate  = AX25KISSInterface.BITRATE_GUESS

        self.packet_queue    = []
        self.flow_control    = flow_control
        self.interface_ready = False
        self.flow_control_timeout = 5
        self.flow_control_locked  = time.time()

        if (len(self.src_call) < 3 or len(self.src_call) > 6):
            raise ValueError("Invalid callsign for "+str(self))

        if (self.src_ssid < 0 or self.src_ssid > 15):
            raise ValueError("Invalid SSID for "+str(self))

        self.preamble    = preamble if preamble != None else 350;
        self.txtail      = txtail if txtail != None else 20;
        self.persistence = persistence if persistence != None else 64;
        self.slottime    = slottime if slottime != None else 20;

        if parity.lower() == "e" or parity.lower() == "even":
            self.parity = serial.PARITY_EVEN

        if parity.lower() == "o" or parity.lower() == "odd":
            self.parity = serial.PARITY_ODD

        try:
            self.open_port()
        except Exception as e:
            RNS.log("Could not open serial port for interface "+str(self), RNS.LOG_ERROR)
            raise e

        if self.serial.is_open:
            self.configure_device()
        else:
            raise IOError("Could not open serial port")

    def open_port(self):
        RNS.log("Opening serial port "+self.port+"...", RNS.LOG_VERBOSE)
        self.serial = self.pyserial.Serial(
            port = self.port,
            baudrate = self.speed,
            bytesize = self.databits,
            parity = self.parity,
            stopbits = self.stopbits,
            xonxoff = False,
            rtscts = False,
            timeout = 0,
            inter_byte_timeout = None,
            write_timeout = None,
            dsrdtr = False,
        )

    def configure_device(self):
        # Allow time for interface to initialise before config
        sleep(2.0)
        thread = threading.Thread(target=self.readLoop)
        thread.daemon = True
        thread.start()
        self.online = True
        RNS.log("Serial port "+self.port+" is now open")
        RNS.log("Configuring AX.25 KISS interface parameters...")
        self.setPreamble(self.preamble)
        self.setTxTail(self.txtail)
        self.setPersistence(self.persistence)
        self.setSlotTime(self.slottime)
        self.setFlowControl(self.flow_control)
        self.interface_ready = True
        RNS.log("AX.25 KISS interface configured")

    def setPreamble(self, preamble):
        preamble_ms = preamble
        preamble = int(preamble_ms / 10)
        if preamble < 0:
            preamble = 0
        if preamble > 255:
            preamble = 255

        kiss_command = bytes([KISS.FEND])+bytes([KISS.CMD_TXDELAY])+bytes([preamble])+bytes([KISS.FEND])
        written = self.serial.write(kiss_command)
        if written != len(kiss_command):
            raise IOError("Could not configure AX.25 KISS interface preamble to "+str(preamble_ms)+" (command value "+str(preamble)+")")

    def setTxTail(self, txtail):
        txtail_ms = txtail
        txtail = int(txtail_ms / 10)
        if txtail < 0:
            txtail = 0
        if txtail > 255:
            txtail = 255

        kiss_command = bytes([KISS.FEND])+bytes([KISS.CMD_TXTAIL])+bytes([txtail])+bytes([KISS.FEND])
        written = self.serial.write(kiss_command)
        if written != len(kiss_command):
            raise IOError("Could not configure AX.25 KISS interface TX tail to "+str(txtail_ms)+" (command value "+str(txtail)+")")

    def setPersistence(self, persistence):
        if persistence < 0:
            persistence = 0
        if persistence > 255:
            persistence = 255

        kiss_command = bytes([KISS.FEND])+bytes([KISS.CMD_P])+bytes([persistence])+bytes([KISS.FEND])
        written = self.serial.write(kiss_command)
        if written != len(kiss_command):
            raise IOError("Could not configure AX.25 KISS interface persistence to "+str(persistence))

    def setSlotTime(self, slottime):
        slottime_ms = slottime
        slottime = int(slottime_ms / 10)
        if slottime < 0:
            slottime = 0
        if slottime > 255:
            slottime = 255

        kiss_command = bytes([KISS.FEND])+bytes([KISS.CMD_SLOTTIME])+bytes([slottime])+bytes([KISS.FEND])
        written = self.serial.write(kiss_command)
        if written != len(kiss_command):
            raise IOError("Could not configure AX.25 KISS interface slot time to "+str(slottime_ms)+" (command value "+str(slottime)+")")

    def setFlowControl(self, flow_control):
        kiss_command = bytes([KISS.FEND])+bytes([KISS.CMD_READY])+bytes([0x01])+bytes([KISS.FEND])
        written = self.serial.write(kiss_command)
        if written != len(kiss_command):
            if (flow_control):
                raise IOError("Could not enable AX.25 KISS interface flow control")
            else:
                raise IOError("Could not enable AX.25 KISS interface flow control")


    def process_incoming(self, data):
        if (len(data) > AX25.HEADER_SIZE):
            self.rxb += len(data)
            self.owner.inbound(data[AX25.HEADER_SIZE:], self)


    def process_outgoing(self,data):
        datalen = len(data)
        if self.online:
            if self.interface_ready:
                if self.flow_control:
                    self.interface_ready = False
                    self.flow_control_locked = time.time()

                encoded_dst_ssid = bytes([0x60 | (self.dst_ssid << 1)])
                encoded_src_ssid = bytes([0x60 | (self.src_ssid << 1) | 0x01])

                addr = b""

                for i in range(0,6):
                    if (i < len(self.dst_call)):
                        addr += bytes([self.dst_call[i]<<1])
                    else:
                        addr += bytes([0x20])
                addr += encoded_dst_ssid

                for i in range(0,6):
                    if (i < len(self.src_call)):
                        addr += bytes([self.src_call[i]<<1])
                    else:
                        addr += bytes([0x20])
                addr += encoded_src_ssid

                data = addr+bytes([AX25.CTRL_UI])+bytes([AX25.PID_NOLAYER3])+data

                data = data.replace(bytes([0xdb]), bytes([0xdb])+bytes([0xdd]))
                data = data.replace(bytes([0xc0]), bytes([0xdb])+bytes([0xdc]))
                kiss_frame = bytes([KISS.FEND])+bytes([0x00])+data+bytes([KISS.FEND])

                written = self.serial.write(kiss_frame)
                self.txb += datalen

                if written != len(kiss_frame):
                    if self.flow_control:
                        self.interface_ready = True
                    raise IOError("AX.25 interface only wrote "+str(written)+" bytes of "+str(len(kiss_frame)))
            else:
                self.queue(data)

    def queue(self, data):
        self.packet_queue.append(data)

    def process_queue(self):
        if len(self.packet_queue) > 0:
            data = self.packet_queue.pop(0)
            self.interface_ready = True
            self.process_outgoing(data)
        elif len(self.packet_queue) == 0:
            self.interface_ready = True

    def readLoop(self):
        try:
            in_frame = False
            escape = False
            command = KISS.CMD_UNKNOWN
            data_buffer = b""
            last_read_ms = int(time.time()*1000)

            while self.serial.is_open:
                if self.serial.in_waiting:
                    byte = ord(self.serial.read(1))
                    last_read_ms = int(time.time()*1000)

                    if (in_frame and byte == KISS.FEND and command == KISS.CMD_DATA):
                        in_frame = False
                        self.process_incoming(data_buffer)
                    elif (byte == KISS.FEND):
                        in_frame = True
                        command = KISS.CMD_UNKNOWN
                        data_buffer = b""
                    elif (in_frame and len(data_buffer) < self.HW_MTU+AX25.HEADER_SIZE):
                        if (len(data_buffer) == 0 and command == KISS.CMD_UNKNOWN):
                            # We only support one HDLC port for now, so
                            # strip off the port nibble
                            byte = byte & 0x0F
                            command = byte
                        elif (command == KISS.CMD_DATA):
                            if (byte == KISS.FESC):
                                escape = True
                            else:
                                if (escape):
                                    if (byte == KISS.TFEND):
                                        byte = KISS.FEND
                                    if (byte == KISS.TFESC):
                                        byte = KISS.FESC
                                    escape = False
                                data_buffer = data_buffer+bytes([byte])
                        elif (command == KISS.CMD_READY):
                            self.process_queue()
                else:
                    time_since_last = int(time.time()*1000) - last_read_ms
                    if len(data_buffer) > 0 and time_since_last > self.timeout:
                        data_buffer = b""
                        in_frame = False
                        command = KISS.CMD_UNKNOWN
                        escape = False
                    sleep(0.05)

                    if self.flow_control:
                        if not self.interface_ready:
                            if time.time() > self.flow_control_locked + self.flow_control_timeout:
                                RNS.log("Interface "+str(self)+" is unlocking flow control due to time-out. This should not happen. Your hardware might have missed a flow-control READY command, or maybe it does not support flow-control.", RNS.LOG_WARNING)
                                self.process_queue()

        except Exception as e:
            self.online = False
            RNS.log("A serial port error occurred, the contained exception was: "+str(e), RNS.LOG_ERROR)
            RNS.log("The interface "+str(self)+" experienced an unrecoverable error and is now offline.", RNS.LOG_ERROR)
            
            if RNS.Reticulum.panic_on_interface_error:
                RNS.panic()

            RNS.log("Reticulum will attempt to reconnect the interface periodically.", RNS.LOG_ERROR)

        self.online = False
        self.serial.close()
        self.reconnect_port()

    def reconnect_port(self):
        while not self.online:
            try:
                time.sleep(5)
                RNS.log("Attempting to reconnect serial port "+str(self.port)+" for "+str(self)+"...", RNS.LOG_VERBOSE)
                self.open_port()
                if self.serial.is_open:
                    self.configure_device()
            except Exception as e:
                RNS.log("Error while reconnecting port, the contained exception was: "+str(e), RNS.LOG_ERROR)

        RNS.log("Reconnected serial port for "+str(self))

    def should_ingress_limit(self):
        return False

    def __str__(self):
        return "AX25KISSInterface["+self.name+"]"