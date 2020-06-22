#!/usr/bin/python
#
# A utility to connect to a VNC server and capture the screen to a video file
#
# Will have a Http interface to start and stop the captures and monitor the sessions
# We want to Connect/Monitor/Disconnect to a server. Then Start and Stop a screen capture to video file
#

import sys, os
import cv2
import numpy as np
import rfb
import threading
import argparse 
import msvcrt  # Windows only!
from PIL import Image
from timeit import default_timer as timer
from twisted.python import usage, log
from twisted.application import internet, service
from twisted.internet import reactor, protocol, endpoints
from twisted.web import server, resource

# Init PIL to make sure it will not try to import plugin libraries
# in a thread.
Image.preinit()
Image.init()

lasterror = "No Error"

class RFBTest(rfb.RFBClient):
    # Class static - we only allow one instance the way we are using it - 
    # hacky, but pythons single threading means we want a single program instance per session recorder so as to spread the CPU load.
    startrecordingflag = False
    stoprecordingflag = False
    recording = False
    videofilename = "output.mp4"
    videofolder = "."

    def vncConnectionMade(self):
        self.screen = None
        self.cursor = None
        self.FirstTime = True
        self.image_mode = "RGBX"

        print("Screen format: depth=%d bytes_per_pixel=%r" % (self.depth, self.bpp))
        print("Desktop name: %r" % self.name)
        rfb.RFBClient.setEncodings(self,[rfb.RAW_ENCODING, rfb.COPY_RECTANGLE_ENCODING ])
        rfb.RFBClient.framebufferUpdateRequest(self)

    def OpenFile(self, filename):        
        print(f"Opening the Video File for writing {filename}")
        SCREEN_SIZE = (1920, 1080) # Do this dynamically later
        fourcc = cv2.VideoWriter_fourcc(*"avc1")    # XVID, H264 - needs openh264-1.8.0-win64.dll , HVEC
        fps = 10.0
        # create the video write object
        self.out = cv2.VideoWriter(filename, fourcc, fps, (SCREEN_SIZE))
        RFBTest.recording = True
        return

    def CloseFile(self):
        # Close off the recorded video file...
        RFBTest.recording = False
        self.out.release()
        print("Closed the Video File")
        return

    def setImageMode(self):
        # Extracts color ordering and 24 vs. 32 bpp info out of the pixel format information
        if self._version_server == 3.889:
            self.setPixelFormat(
                    bpp = 16, depth = 16, bigendian = 0, truecolor = 1,
                    redmax = 31, greenmax = 63, bluemax = 31,
                    redshift = 11, greenshift = 5, blueshift = 0
                    )
            self.image_mode = "BGR;16"
        elif (self.truecolor and (not self.bigendian) and self.depth == 24
                and self.redmax == 255 and self.greenmax == 255 and self.bluemax == 255):

            pixel = ["X"] * self.bypp
            offsets = [offset // 8 for offset in (self.redshift, self.greenshift, self.blueshift)]
            for offset, color in zip(offsets, "RGB"):
                pixel[offset] = color
            self.image_mode = "".join(pixel)
        else:
            self.setPixelFormat()

    def updateCursor(self, x, y, width, height, image, mask):
        if self.factory.nocursor:
            return

        if not width or not height:
            self.cursor = None

        self.cursor = Image.frombytes('RGBX', (width, height), image)
        self.cmask = Image.frombytes('1', (width, height), mask)
        self.cfocus = x, y
        self.drawCursor()

    def drawCursor(self):
        if not self.cursor:
            return

        if not self.screen:
            return

        x = self.x - self.cfocus[0]
        y = self.y - self.cfocus[1]
        self.screen.paste(self.cursor, (x, y), self.cmask)

    def updateRectangle(self, x, y, width, height, data):
        # print(f"Update Rectangle ({x},{y}), {width}, {height} ")
        # ignore empty updates
        if not data:
            return

        size = (width, height)
        update = Image.frombytes('RGB', size, data, 'raw', self.image_mode)
        if not self.screen:
            self.screen = update
        # track upward screen resizes, often occurs during os boot of VMs
        # When the screen is sent in chunks (as observed on VMWare ESXi), the canvas
        # needs to be resized to fit all existing contents and the update.
        elif self.screen.size[0] < (x+width) or self.screen.size[1] < (y+height):
            new_size = (max(x+width, self.screen.size[0]), max(y+height, self.screen.size[1]))
            new_screen = Image.new("RGB", new_size, "black")
            new_screen.paste(self.screen, (0, 0))
            new_screen.paste(update, (x, y))
            self.screen = new_screen
        else:
            self.screen.paste(update, (x, y))

        self.drawCursor()       
            
    def beginUpdate(self):
        # called before a series of updateRectangle(), copyRectangle() or fillRectangle().
        # Probably prevent trying to get a copy of the image to add to the video file at this point.
        # Unlock on the commitupdate. Otherwise likely to get wonky images.
        # print(f"Begin Update")
        return

    def commitUpdate(self, rectangles=None):
        # called after a series of updateRectangle(), copyRectangle() or fillRectangle() are finished.
        # typicaly, here is the place to request the next screen update with FramebufferUpdateRequest(incremental=1).
        # argument is a list of tuples (x,y,w,h) with the updated rectangles.
    
        # Every 100msec we should dump the screen image into a queue, so we dont hold this up, 
        # and the main loop can write the queue to the video file.
        # Would make opening and closing the video file simpler and cleaner. Try and avoid a memory copy...
        # Need a timer to see if we have waited 100msec, or we got here sooner. Maybe the request rate - trigger update should be 2 x the fps.

        

        if (self.FirstTime):            
            self.start = timer()
            self.FirstTime = False     
            reactor.callLater(0.1, self.triggerupdate)    # 100msec, 10 per sec (calls itself from that pont on..)       
        else:
            # Only write out the buffer every 100msec
            end = timer()
            if (end - self.start >= 0.1):
                self.start += 0.1
                # We may not always be capturing the session
                if (self.recording == True):
                    frame = np.array(self.screen)   # Convert PIL image to OpenCV image
                    self.out.write(frame)    # Write the frame to the video file
        return

    # Self calling function to run every 100msec.
    def triggerupdate(self):
        if (RFBTest.startrecordingflag == True):   # Thread protection???
            RFBTest.startrecordingflag = False
            self.OpenFile(os.path.join(RFBTest.videofolder, RFBTest.videofilename))

        if (RFBTest.stoprecordingflag == True):   # Thread protection???
            RFBTest.stoprecordingflag = False
            self.CloseFile()

        rfb.RFBClient.framebufferUpdateRequest(self,incremental=1)
        reactor.callLater(0.1, self.triggerupdate)
        return

class RFBTestFactory(rfb.RFBFactory):

    def __init__(self, password = None, shared = 0):
        #self.deferred = Deferred()
        self.protocol = RFBTest
        self.password = password
        self.shared = shared

    def clientConnectionLost(self, connector, reason):
        lasterror = f"Connection lost: {reason}"
        print(lasterror)
        try:
            self.protocol.CloseFile(self.protocol)
            connector.connect()         # Try re-establishing the connection - depending on reason???

        except Exception as e:
            # woa, this means that something bad happened,
            # most probably we received a SIGINT. Now this is only
            # a problem when you use Ctrl+C to stop the main process
            # because it would send the SIGINT to child processes too.
            # In all other cases receiving a SIGINT here would be an
            # error condition and correctly restarted. maybe we should
            # use sigprocmask?
            print("Exception: ReactorNotRunning - Ignoring")
            pass             

    def clientConnectionFailed(self, connector, reason):
        lasterror = f"Connection failed: {reason}"
        print(lasterror)
        self.protocol.CloseFile(self.protocol)
        reactor.stop()
        
def mainloop( dum=None):
    # gui 'mainloop', it is called repeated by twisteds mainloop by using callLater
    print(".",end='')
    no_work = False

    if msvcrt.kbhit():
        key = msvcrt.getch()
        print(key) 
        if (key == b'q'):
            print('Exiting')
            no_work = True
        elif (key == b'S'):
            print('Start Recording')
            RFBTest.startrecordingflag = True
        elif (key == b's'):
            print('Stop Recording')
            RFBTest.stoprecordingflag = True
        else:
            print("Only valid keys are 'q', 'S'tart recording, 's'top recording")
    
    if (not no_work):
        reactor.callLater(2, mainloop)
    else:
        reactor.callLater(0.01, reactor.stop)

# Start of Main
log.startLogging(sys.stdout)

parser = argparse.ArgumentParser() 
parser.add_argument("-hp", dest='httpport', default=5001, type=int, help = "HTTP Listen Port")
parser.add_argument("-vt", dest='vncserver', default='localhost', help = "VNC Target IP Address")
parser.add_argument("-vf", dest='videofolder', default='v:\WS10', help = "Target Video Folder")
parser.add_argument("-pwd", dest='password', default='Energy123', help = "VNC Password")
args = parser.parse_args() 

RFBTest.videofolder = args.videofolder

application = service.Application("rfb test") # create Application

# connect to this host and port, and reconnect if we get disconnected
vncClient = internet.TCPClient(args.vncserver, 5900, RFBTestFactory(password=args.password)) # create the service
vncClient.setServiceParent(application)
vncClient.startService()

class Web(resource.Resource):
    isLeaf = True
    def render_GET(self, request):

        if (request.path == b'/startrecord'):
            filename = request.args.get(b'filename')
            if (filename is not None):
                RFBTest.videofilename = filename[0].decode('utf-8')
                RFBTest.startrecordingflag = True
                return f"<html>Start Recording to {RFBTest.videofilename}</html>".encode('utf-8')
            else:
                return f"<html>Start Recording Failed, missing filename parameter</html>".encode('utf-8')

        if (request.path == b'/stoprecord'):
            RFBTest.stoprecordingflag = True
            RFBTest.videofilename = None
            return "<html>Stopped Recording</html>".encode('utf-8')

        if (request.path == b'/'):
            return f"<html>Remote Capture (VNC) Server for VNC Client {args.vncserver}, <br>Last Error: {lasterror}<br>Currently Recording: {RFBTest.recording}</html>".encode('utf-8')

        return f"<html>Remote Capture (VNC) Server for VNC Client {args.vncserver}, Illegal Path {request.path}</html>".encode('utf-8')

resource = Web()
resource.putChild(b'startrecord', Web())
resource.putChild(b'stoprecord', Web())
site = server.Site(resource)
endpoint = endpoints.TCP4ServerEndpoint(reactor, args.httpport)
endpoint.listen(site)

reactor.callLater(0.2, mainloop)    # 200msec later..
#reactor.callLater(60, reactor.stop) # Only run for a minute - how we exit...

reactor.run()  

print("Main Program Exit\n")    


