#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import nnpy
import dill
from threading import Thread
from queue import Queue
import json

from trading_ig.lightstreamer import LSClient, Subscription

logger = logging.getLogger(__name__)

SUB_TRADE_CONFIRMS = 'inproc://sub_trade_confirms'
SUB_TRADE_OPU = 'inproc://sub_trade_opu'
SUB_TRADE_WOU = 'inproc://sub_trade_wou'


class IGStreamService(object):
    def __init__(self, ig_service):
        self.ig_service = ig_service
        self.ig_session = None
        self.ls_client = None

    def create_session(self):
        ig_session = self.ig_service.create_session()
        self.ig_session = ig_session
        return ig_session

    def connect(self, accountId):
        cst = self.ig_service.crud_session.CLIENT_TOKEN
        xsecuritytoken = self.ig_service.crud_session.SECURITY_TOKEN
        lightstreamerEndpoint = self.ig_session[u'lightstreamerEndpoint']
        # clientId = self.ig_session[u'clientId']
        ls_password = 'CST-%s|XST-%s' % (cst, xsecuritytoken)

        # Establishing a new connection to Lightstreamer Server
        logger.info("Starting connection with %s" % lightstreamerEndpoint)
        # self.ls_client = LSClient("http://localhost:8080", "DEMO")
        # self.ls_client = LSClient("http://push.lightstreamer.com", "DEMO")
        self.ls_client = LSClient(lightstreamerEndpoint, adapter_set="",
                                  user=accountId, password=ls_password)
        try:
            self.ls_client.connect()
        except Exception as exc:
            logger.exception("Unable to connect to Lightstreamer Server")
            raise exc

        # Create subsciption channel for trade events
        self._create_subscription_channels(accountId)

    def _create_subscription_channels(self, accountId):
        """
        Function to create a subscription with the Lightstream server and
        create a local publish/subscription system to read those events when
        they are needed using the 'wait_event' function.
        """
        self.publishers = []
        subscription = Subscription(
            mode="DISTINCT",
            items=["TRADE:%s" % accountId],
            fields=["CONFIRMS", "OPU", "WOU"])

        pub_confirms = nnpy.Socket(nnpy.AF_SP, nnpy.PUB)
        pub_confirms.bind(SUB_TRADE_CONFIRMS)
        self.publishers.append(pub_confirms)

        pub_opu = nnpy.Socket(nnpy.AF_SP, nnpy.PUB)
        pub_opu.bind(SUB_TRADE_OPU)
        self.publishers.append(pub_opu)

        pub_wou = nnpy.Socket(nnpy.AF_SP, nnpy.PUB)
        pub_wou.bind(SUB_TRADE_WOU)
        self.publishers.append(pub_wou)

        def on_item_update(data):
            logger.info(data)
            values = data.get('values', {})
            # Publish confirms
            event = values.get('CONFIRMS')
            if event:
                pub_confirms.send(dill.dumps(event))
            # Publish opu
            event = values.get('OPU')
            if event:
                pub_opu.send(dill.dumps(event))
            # Publish wou
            event = values.get('WOU')
            if event:
                pub_wou.send(dill.dumps(event))

        subscription.addlistener(on_item_update)
        self.ls_client.subscribe(subscription)

    def unsubscribe_all(self):
        # To avoid a RuntimeError: dictionary changed size during iteration
        subscriptions = self.ls_client._subscriptions.copy()
        for subcription_key in subscriptions:
            self.ls_client.unsubscribe(subcription_key)

    def disconnect(self):
        logging.info("Disconnect from the light stream.")
        for publisher in self.publishers:
            try:
                publisher.close()
            except Exception:
                logging.exception("Failed to close publisher %s", publisher)
        self.publishers = []
        self.unsubscribe_all()
        self.ls_client.disconnect()


class ChannelClosedException(Exception):
    msg = "Channel is already closed. Create a new channel for new events."

    def __str__(self):
        return self.msg


class Channel:
    def __init__(self, channel):
        self.channel = channel
        # Subscribe to channel
        self.sub = nnpy.Socket(nnpy.AF_SP, nnpy.SUB)
        self.sub.connect(self.channel)
        self.sub.setsockopt(nnpy.SUB, nnpy.SUB_SUBSCRIBE, '')
        # Create queue and start updating it
        self.queue = Queue()
        Thread(target=self._update_queue).start()

    def _update_queue(self):
        while True:
            try:
                data = json.loads(dill.loads(self.sub.recv()))
                self.queue.put(data)
            except nnpy.errors.NNError:
                break

    def _process_queue(self, function):
        data = None
        while True:
            data = self.queue.get()
            if function(data):
                break
        return data

    def wait_event(self, key, value):
        logging.info("Wait for event '%s' == '%s'", key, value)
        if not self.sub:
            raise ChannelClosedException
        event = self._process_queue(lambda v: v[key] == value)
        self.sub.close()  # Close subscriber to stop _update_queue
        self.sub = None  # Disable subscriber so it doesn't get called any more
        return event


class ConfirmChannel(Channel):
    def __init__(self):
        super().__init__(SUB_TRADE_CONFIRMS)


class OPUChannel(Channel):
    def __init__(self):
        super().__init__(SUB_TRADE_OPU)


class WOUChannel(Channel):
    def __init__(self):
        super().__init__(SUB_TRADE_WOU)
