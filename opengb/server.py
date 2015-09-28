"""
OpenGB server.

This is the core of openGB. It creates a printer object to run in a separate
process and communicates with it via queues.
"""

import os
import sys
import json
import logging
import multiprocessing
from pkg_resources import Requirement, resource_filename

import tornado.httpserver
import tornado.ioloop
import tornado.log
import tornado.websocket
from tornado.options import options
from tornado.web import Application, RequestHandler, StaticFileHandler
from jsonrpc import JSONRPCResponseManager, Dispatcher 

import opengb.config
import opengb.printer
import opengb.database


# TODO: use rotated file logging.
LOGGER = tornado.log.app_log

# Websocket clients.
CLIENTS = []

# To/from printer queues.
TO_PRINTER = multiprocessing.Queue()
FROM_PRINTER = multiprocessing.Queue()

# Local cache of printer state.
PRINTER = {
    'state':    opengb.printer.State.DISCONNECTED,
    'temp':     {
        'bed':      0,
        'nozzle1':  0,
        'nozzle2':  0,
    },
    'progress': {
        'current':  0,
        'total':    0,
    },
}


class MessageHandler(object):
    """
    Handles JSON-RPC calls received via websocket.
    """

    def __init__(self):
        pass

    def set_temp(self, bed=None, nozzle1=None, nozzle2=None):
        """
        Set printer target temperatures.

        Unspecified target temperatures will remain unchanged.

        :param bed: Bed target temperature. 
        :type bed: :class:`float`
        :param nozzle1: Nozzle1 target temperature. 
        :type nozze1: :class:`float`
        :param nozzle1: Nozzle2 target temperature. 
        :type nozze1: :class:`float`
        """
        TO_PRINTER.put(json.dumps({
            'method':   'set_temp',
            'params': {
                'bed':      bed,
                'nozzle1':  nozzle1,
                'nozzle2':  nozzle2
            }
        }))
        return True

class WebSocketHandler(tornado.websocket.WebSocketHandler):
    """
    Handles all websocket communication with clients.

    Uses `json-rpc <https://pypi.python.org/pypi/json-rpc/>`_ to map messages
    to methods and generate valid JSON-RPC 2.0 responses.
    """

    def __init__(self, *args, **kwargs):
        message_handler = MessageHandler()
        self.dispatcher = Dispatcher(message_handler)
        super().__init__(*args, **kwargs)

    def open(self):
        LOGGER.info('New connection from {0}'.format(self.request.remote_ip))
        CLIENTS.append(self)
        self.write_message(json.dumps(
            {'cmd': 'STATE', 'new': PRINTER['state']},
            cls=opengb.printer.StateEncoder))

    def on_close(self):
        LOGGER.info('Connection closed to {0}'.format(self.request.remote_ip))
        CLIENTS.remove(self)

    def on_message(self, message):
        """
        Passes an incoming JSON-RPC message to the dispatcher for processing.
        """
        LOGGER.debug('Message received from {0}: {1}'.format(
            self.request.remote_ip, message))
        response = JSONRPCResponseManager.handle(message, self.dispatcher)
        LOGGER.debug('Sending response to {0}: {1}'.format(
                self.request.remote_ip, response._data))
        return response.json


class StatusHandler(RequestHandler):
    def get(self):
        self.write(json.dumps(PRINTER, cls=opengb.printer.StateEncoder))
        self.set_header("Content-Type", "application/json") 


def broadcast_message(message):
    """
    Broadcast message to websocket clients.
    """
    for each in CLIENTS:
        each.write_message(message)


def process_event(event):
    """
    Process an event from the printer.
    """
    global PRINTER
    try:
        if event['method'] == 'state':
            # TODO: if state changes from printing to ready, reset progress.
            PRINTER['state'] = opengb.printer.State(event['params']['new'])
        elif event['method'] == 'temp':
            PRINTER['temp'] = event['params'] 
        elif event['method'] == 'progress':
            PRINTER['progress'] = event['params']
        elif event['method'] == 'zchange':
            # TODO: trigger update camera image. 
            pass
    except KeyError as e:
        LOGGER.error('Malformed event from printer: {0}'.format(event))


def process_printer_events():
    """
    Process events from printer.

    Runs via a :class:`tornado.ioloop.PeriodicCallback`.
    """
    if not FROM_PRINTER.empty():
        try:
            event = json.loads(FROM_PRINTER.get())
            if event['method'] == 'log':
                LOGGER.log(event['params']['level'], event['params']['msg'])
            else:
                broadcast_message(event)
                process_event(event)
        except TypeError as e:
            LOGGER.exception(e)


def main():
    # Load config.
    options.parse_config_file(opengb.config.CONFIG_FILE)

    # Initialise database.
    opengb.database.initialize(options.db_file)

    # Initialize printer using queue callbacks.
    printer_callbacks = opengb.printer.QueuedPrinterCallbacks(FROM_PRINTER)
    printer_type = getattr(opengb.printer, options.printer)
    printer = printer_type(TO_PRINTER, printer_callbacks,
                           baud_rate=options.baud_rate, port=options.serial_port)
    printer.daemon = True
    printer.start()

    # Initialize web server.
    install_dir = resource_filename(Requirement.parse('openGB'), 'opengb')
    static_dir = os.path.join(install_dir, 'static')
    handlers = [
        (r"/ws", WebSocketHandler),
        (r"/api/status", StatusHandler),
        (r"/fonts/(.*)", StaticFileHandler, {"path": os.path.join(static_dir, "fonts")}),
        (r"/img/(.*)", StaticFileHandler, {"path": os.path.join(static_dir, "img")}),
        (r"/js/(.*)", StaticFileHandler, {"path": os.path.join(static_dir, "js")}),
        (r"/css/(.*)", StaticFileHandler, {"path": os.path.join(static_dir, "css")}),
        (r"/(.*)", StaticFileHandler, {"path": os.path.join(static_dir, "index.html")}),
    ]
    app = Application(handlers=handlers, debug=options.debug)
    httpServer = tornado.httpserver.HTTPServer(app)
    httpServer.listen(options.http_port)

    # Rock and roll.
    main_loop = tornado.ioloop.IOLoop.instance()
    printer_event_processor = tornado.ioloop.PeriodicCallback(
        lambda: process_printer_events(), 10, io_loop=main_loop)
    # TODO: ioloop for watchdog
    # TODO: ioloop for camera
    printer_event_processor.start()
    main_loop.start()

    return(os.EX_OK)

if __name__ == '__main__':
    sys.exit(main())
