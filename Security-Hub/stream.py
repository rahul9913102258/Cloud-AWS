"""
stream.py

Individual thread for event stream processing
"""
import datetime
import time
import json
import re
import requests
from falconpy import APIHarness
import logger


class Stream():  # pylint: disable=R0902
    """Class to represent a single stream."""
    def __init__(self, stream_config, api_interface, sqs_queue, config, current_cid):  # pylint: disable=R0913
        self.data_feed = stream_config["dataFeedURL"]
        self.token = stream_config["sessionToken"]["token"]
        self.token_expiration = stream_config["sessionToken"]["expiration"]
        self.refresh_url = stream_config["refreshActiveSessionURL"]
        self.refresh_interval = stream_config["refreshActiveSessionInterval"]
        # Calculate our base URL
        result = re.match("^(http://|https://)([^#?/]+)", self.refresh_url)
        if result:
            self.base_url = result.group(0)
        else:
            # Fallback to commercial if we can't calculate it
            self.base_url = "https://api.crowdstrike.com"

        self.time_one = datetime.datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S +0000')
        self.headers = {'Authorization': f'Token {self.token}', 'Date': self.time_one, 'Connection': 'Keep-Alive'}
        self.discarded = 0
        self.detections = 0
        self.processed = 0
        self.epoch = int(time.time())
        self.app_id = config["app_id"]
        self.severity_threshold = config["severity_threshold"]
        self.api_interface = api_interface
        self.current_cid = current_cid
        self.api_config = config
        self.sqs_queue = sqs_queue
        self.partition = self.refresh_url.replace(
            f"{self.base_url}/sensors/entities/datafeed-actions/v1/",
            ""
            ).replace(
                f"?appId={str(self.app_id).lower()}&action_name=refresh_active_stream_session",
                ""
            )
        # Log, offset and PID files
        self.log_file = f"{str(self.app_id).lower()}_{str(self.partition)}.log"
        self.offset_file = f".{str(self.app_id).lower()}_{str(self.partition)}.offset"
        # Might not use this
        self.process_id = f"{str(self.app_id)}_{str(self.partition)}.pid"
        self.logger = logger.Logger(self.log_file, f"stream{str(self.partition)}")

        # Retrieve our offset if it's been stashed previously
        self.offset = self.logger.offset_read(self.offset_file)
        # Find our old position in the stream
        if self.offset > 0:
            self.data_feed = self.data_feed + f"&offset={str(self.offset)}"
        # Our active spout
        self.spigot = False
        # Token reporting lambdas
        self.token_expired = bool(int(time.time()) > ((int(self.refresh_interval)-60)+self.epoch))
        self.token_remains = lambda: ((int(self.refresh_interval)-60)+self.epoch)-int(time.time())
        # Thread running flag
        self.running = True

    def __enter__(self):
        """Enter handler."""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Exit handler."""
        self.logger.status_write("Stream exiting.")

    def to_json(self):
        """Return the entire object in JSON format."""
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True)

    def set_offset(self, offset):
        """Write the offset value to the offset log."""
        self.offset = offset
        self.logger.offset_write(self.offset_file, offset)
        return True

    def open(self):
        """Open the stream."""
        kwargs = {
            "url": self.data_feed,
            "headers": self.headers,
            "stream": True
        }
        if not self.api_config["ssl_verify"]:
            kwargs["verify"] = False

        self.spigot = requests.get(**kwargs)

        return self.spigot

    def close(self):
        """Close the stream."""
        self.spigot.close()
        self.spigot = False

        return True

    def refresh(self):
        """Refresh the API stream token."""
        refresher = self.api_interface.command(action="refreshActiveStreamSession", partition=self.partition, parameters={
           "action_name": "refresh_active_stream_session",
           "appId": str(self.app_id).lower()
        })
        refreshed = False

        if "status_code" in refresher:
            if refresher["status_code"] == 200:
                refreshed = True
                self.epoch = int(time.time())
                self.logger.status_write("Token refreshed.")

        return refreshed

    @staticmethod
    def create_payload(resource_detail, decoded_line):
        """Create our SQS payload."""
        utc = datetime.datetime.utcfromtimestamp
        create_time = utc(float(decoded_line['metadata']['eventCreationTime'])/1000.).isoformat()+'Z'
        update_time = (utc(datetime.datetime.timestamp(datetime.datetime.now()))).isoformat()+'Z'
        payload = {
            "hostname": resource_detail["hostname"],
            "detected_mac_address": decoded_line["event"]["MACAddress"],
            "detected_local_ip": decoded_line["event"]["LocalIP"],
            "detection_id": decoded_line["event"]["DetectId"],
            "tactic": decoded_line["event"]["Tactic"],
            "technique": decoded_line["event"]["Technique"],
            "description": decoded_line["event"]["DetectDescription"],
            "source_url": decoded_line["event"]["FalconHostLink"],
            "generator_id": "Falcon Host",
            "types": ["Namespace: Threat Detections"],
            "created_at": (create_time),
            "updated_at": (update_time),
            "record_state": "ACTIVE",
            "severity": decoded_line["event"]["Severity"]
        }

        if "instance_id" in resource_detail:
            payload["instance_id"] = resource_detail["instance_id"]
        if "service_provider_account_id" in resource_detail:
            payload["service_provider_account_id"] = resource_detail["service_provider_account_id"]
        if "local_ip" in resource_detail:
            payload["local_ip"] = resource_detail["local_ip"]
        if "mac_address" in resource_detail:
            payload["mac_address"] = resource_detail["mac_address"]
        try:
            payload["Process"] = {}
            payload["Process"]["Name"] = decoded_line["event"]["FileName"]
            payload["Process"]["Path"] = decoded_line["event"]["FilePath"]
        except KeyError:
            payload.pop("Process", None)

        try:
            payload['Network'] = {}
            net_traffic = decoded_line['event']['NetworkAccesses'][0]
            payload['Network']['Direction'] = "IN" if net_traffic['ConnectionDirection'] == 0 else 'OUT'
            payload['Network']['Protocol'] = net_traffic['Protocol']
            payload['Network']['SourceIpV4'] = net_traffic['LocalAddress']
            payload['Network']['SourcePort'] = net_traffic['LocalPort']
            payload['Network']['DestinationIpV4'] = net_traffic['RemoteAddress']
            payload['Network']['DestinationPort'] = net_traffic['RemotePort']
        except (KeyError, IndexError):
            payload.pop('Network', None)

        return payload

    @staticmethod
    def get_compare_values(decoded_l: dict, resource_det: dict):
        """Retrieve the values for our comparison."""
        resource_m = "right"
        try:
            decoded_m = decoded_l["event"]["MACAddress"].lower().replace(":", "").replace("-", "")
            resource_m = resource_det["mac_address"].lower().replace(":", "").replace("-", "")
        except KeyError:
            # Any other option falls back to hostname
            decoded_m = decoded_l["event"]["ComputerName"].lower().replace(":", "").replace("-", "")
            try:
                resource_m = resource_det["hostname"].lower().replace(":", "").replace("-", "")
            except KeyError:
                pass

        # print(f"{decoded_m} vs {resource_m}")
        return decoded_m, resource_m

    def check_records(self, decode: dict, sensor: str, api: object, payload: str or bool):
        """Retrieve our record batch, display the results and update our positions"""

        reviewed = 0
        host_lookup = api.command(action="GetDeviceDetails", ids=sensor)
        for resource_detail in host_lookup["body"]["resources"]:
            reviewed += 1
            if self.api_config["confirm_provider"]:
                if "service_provider" in resource_detail:
                    if resource_detail["service_provider"] == "AWS_EC2":
                        decoded_mac, resource_mac = self.get_compare_values(decode, resource_detail)
                        if decoded_mac == resource_mac:
                            payload = self.create_payload(resource_detail, decode)
                            # Hit
                            self.detections += 1
                    else:
                        # Miss - not in AWS
                        self.discarded += 1
                else:
                    # Miss - no svc provider
                    self.discarded += 1
            else:
                decoded_mac, resource_mac = self.get_compare_values(decode, resource_detail)
                if decoded_mac == resource_mac:
                    payload = self.create_payload(resource_detail, decode)
                    # Hit
                    self.detections += 1
        if reviewed == 0:
            self.logger.status_write("No hosts returned from API that match the reported detection. "
                                     "Confirm your API credentials have access to the Falcon Hosts API."
                                     )
        return payload

    def parse(self, line):
        """Parse the received line."""
        # Return a false if this is a miss
        payload = False
        decoded_line = json.loads(line.decode("utf-8"))
        # Grab our current offset
        cur_offset = decoded_line["metadata"]["offset"]
        # Detections only
        if decoded_line["metadata"]["eventType"] == "DetectionSummaryEvent":
            # Only submit detections that meet our severity threshold
            if int(self.severity_threshold) <= int(decoded_line["event"]["Severity"]):
                creds = {
                    "client_id": self.api_config["falcon_client_id"],
                    "client_secret": self.api_config["falcon_client_secret"]
                }
                # Is this a child detection?
                member_cid = self.current_cid.lower()
                if "customerIDString" in decoded_line["metadata"]:
                    member_cid = decoded_line["metadata"]["customerIDString"].lower()
                if member_cid != self.current_cid.lower():
                    creds["member_cid"] = member_cid
                # Connect to the Hosts API and check all hosts that match this sensor
                falcon = APIHarness(creds=creds, base_url=self.base_url)
                sensor = decoded_line["event"]["SensorId"]
                # print(sensor)
                payload = self.check_records(decoded_line, sensor, falcon, payload)

            else:
                # Miss - below severity threshold
                self.discarded += 1
        else:
            # Miss - not a detection
            self.discarded += 1

        return payload, cur_offset

    def force_quit(self):
        """Force the thread to quit."""
        self.logger.status_write("Thread quitting.")
        return True

    def process(self):
        """Process the thread until told to quit."""
        while self.running:
            with self.open() as stream:
                logtext = "started" if self.offset == 0 else "resumed"
                self.logger.status_write(f"Stream {logtext} at position {self.offset}")
                for line in stream.iter_lines():
                    if self.running:
                        if line:
                            parsed, cur_offset = self.parse(line)
                            if parsed:
                                # print(parsed)
                                response = self.sqs_queue.send_message(MessageBody=json.dumps(parsed))
                                m_id = str(response.get("MessageId"))
                                self.logger.status_write(f"Sending detection to SQS. (Message ID: {m_id})")
                                self.logger.output_write(parsed)
                            # Update our position in the stream
                            self.set_offset(cur_offset)
                        # Are we close to exceeding our epoch window? (refresh 1 minute before)
                        if self.token_expired:
                            self.refresh()
                    else:
                        self.set_offset(cur_offset)
                        break
                else:
                    continue
                break
        else:
            pass

        self.force_quit()

        raise SystemExit("Thread terminated")
