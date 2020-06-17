#!/usr/bin/python
#
# A utility to connect to a VNC server and capture the screen to a video file
#
# Will have a Http interface to start and stop the captures and monitor the sessions
# We want to Connect/Monitor/Disconnect to a server. Then Start and Stop a screen capture to video file
#
# Use flask for the http interface, but use app.run() so we actually run the python file.


import sys, os
import flask
import cv2
import numpy as np
import rfb
import msvcrt  # Windows only!
from PIL import Image
from twisted.python import usage, log
from twisted.internet.protocol import Protocol
from twisted.internet import protocol
from twisted.application import internet, service
from twisted.internet import reactor


# Init PIL to make sure it will not try to import plugin libraries
# in a thread.
Image.preinit()
Image.init()

class RFBTest(rfb.RFBClient):
    """Test client"""

    def vncConnectionMade(self):
        self.screen = None
        self.cursor = None
        self.FirstTime = True
        self.image_mode = "RGBX"

        print("Screen format: depth=%d bytes_per_pixel=%r" % (self.depth, self.bpp))
        print("Desktop name: %r" % self.name)
        rfb.RFBClient.setEncodings(self,[rfb.RAW_ENCODING, rfb.COPY_RECTANGLE_ENCODING ])
        rfb.RFBClient.framebufferUpdateRequest(self)

    def CloseFile(self):
        # Close off the recorded video file...
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
    
        # Just repeat the number of previous frames to get to the current time stamp, then add new frame.
        # Use a frame rate of 10 per second? Does not need to be too fast!
        # As long as the delay from VNC is reasonably consistent, we should get a good result.
        # print(f"Commit Update")
        frame = np.array(self.screen)   # Convert PIL image to OpenCV image
        out.write(frame)    # Write the frame to the video file

        if (self.FirstTime):
            self.FirstTime = False
            reactor.callLater(0.01, self.triggerupdate)    # 100msec, 10 per sec

        return

    def triggerupdate(self):
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
        print(reason)
        try:
            reactor.stop()
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
        print("connection failed:", reason)
        self.protocol.CloseFile()
        reactor.stop()
        
        
def mainloop( dum=None):
    # gui 'mainloop', it is called repeated by twisteds mainloop by using callLater
    print("Main Loop")
    no_work = False

    if msvcrt.kbhit():
        key = msvcrt.getch()
        print(key) 
        if (key == b'q'):
            print('Exiting')
            no_work = True
        else:
            print('You Pressed A Key! Hit q to exit')

    if (not no_work):
        reactor.callLater(2, mainloop)
    else:
        reactor.callLater(0.01, reactor.stop)

log.startLogging(sys.stdout)

application = service.Application("rfb test") # create Application

# connect to this host and port, and reconnect if we get disconnected
vncClient = internet.TCPClient("localhost", 5900, RFBTestFactory(password="Hunter20")) # create the service
vncClient.setServiceParent(application)
vncClient.startService()
reactor.callLater(0.2, mainloop)    # 200msec later..
reactor.callLater(60, reactor.stop) # Only run for a minute - how we exit...


SCREEN_SIZE = (1920, 1080) # Do this dynamically later
fourcc = cv2.VideoWriter_fourcc(*"XVID")    # XVID, H264, HVEC
fps = 10.0
# create the video write object
out = cv2.VideoWriter("output.mp4", fourcc, fps, (SCREEN_SIZE))

reactor.run()        

#cv2.destroyAllWindows()
out.release()
