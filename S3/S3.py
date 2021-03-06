## Amazon S3 manager
## Author: Michal Ludvig <michal@logix.cz>
##         http://www.logix.cz/michal
## License: GPL Version 2

import sys
import os, os.path
import time
import httplib
import logging
import mimetypes
import re
import Queue
import threading
from logging import debug, info, warning, error
from stat import ST_SIZE

try:
    from hashlib import md5
except ImportError:
    from md5 import md5

from Utils import *
from SortedDict import SortedDict
from BidirMap import BidirMap
from Config import Config
from Utils import concat_files, hash_file_md5 
from Exceptions import *
from ACL import ACL, GranteeLogDelivery
from AccessLog import AccessLog
from S3Uri import S3Uri

__all__ = []
class S3Request(object):
    def __init__(self, s3, method_string, resource, headers, params = {}):
        self.s3 = s3
        self.headers = SortedDict(headers or {}, ignore_case = True)
        self.resource = resource
        self.method_string = method_string
        self.params = params

        self.update_timestamp()
        self.sign()

    def update_timestamp(self):
        if self.headers.has_key("date"):
            del(self.headers["date"])
        self.headers["x-amz-date"] = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())

    def format_param_str(self):
        """
        Format URL parameters from self.params and returns
        ?parm1=val1&parm2=val2 or an empty string if there
        are no parameters.  Output of this function should
        be appended directly to self.resource['uri']
        """
        param_str = ""
        for param in self.params:
            if self.params[param] not in (None, ""):
                param_str += "&%s=%s" % (param, self.params[param])
            else:
                param_str += "&%s" % param
        return param_str and "?" + param_str[1:]

    def sign(self):
        h  = self.method_string + "\n" 
        h += self.headers.get("content-md5", "")+"\n"
        h += self.headers.get("content-type", "")+"\n"
        h += self.headers.get("date", "")+"\n"
        for header in self.headers.keys():
            if header.startswith("x-amz-"):
                h += header+":"+str(self.headers[header])+"\n"
        if self.resource['bucket']:
            h += "/" + self.resource['bucket']
        h += self.resource['uri']

        tmp_params = "" 
        for parameter in self.params:
            if parameter in ['uploads', 'partNumber', 'uploadId', 'acl', 'location', 'logging', 'torrent']:
                if self.params[parameter] != "":
                    tmp_params += '&%s=%s' %(parameter, self.params[parameter])
                else:
                    tmp_params += '&%s' %parameter 
        if tmp_params != "":
            h+='?'+tmp_params[1:].encode('UTF-8')
        debug("SignHeaders: " + repr(h))
        signature = sign_string(h)
        self.headers["Authorization"] = "AWS "+self.s3.config.access_key+":"+signature

    def get_triplet(self):
        self.update_timestamp()
        self.sign()
        resource = dict(self.resource)  ## take a copy
        resource['uri'] += self.format_param_str()
        return (self.method_string, resource, self.headers)

class S3(object):
    http_methods = BidirMap(
        GET = 0x01,
        PUT = 0x02,
        HEAD = 0x04,
        DELETE = 0x08,
        POST = 0x20,
        MASK = 0xFF,
        )

    targets = BidirMap(
        SERVICE = 0x0100,
        BUCKET = 0x0200,
        OBJECT = 0x0400,
        MASK = 0x0700,
        )

    operations = BidirMap(
        UNDFINED = 0x0000,
        LIST_ALL_BUCKETS = targets["SERVICE"] | http_methods["GET"],
        BUCKET_CREATE = targets["BUCKET"] | http_methods["PUT"],
        BUCKET_LIST = targets["BUCKET"] | http_methods["GET"],
        BUCKET_DELETE = targets["BUCKET"] | http_methods["DELETE"],
        OBJECT_PUT = targets["OBJECT"] | http_methods["PUT"],
        OBJECT_GET = targets["OBJECT"] | http_methods["GET"],
        OBJECT_HEAD = targets["OBJECT"] | http_methods["HEAD"],
        OBJECT_POST = targets["OBJECT"] | http_methods["POST"],
        OBJECT_DELETE = targets["OBJECT"] | http_methods["DELETE"],
    )

    codes = {
        "NoSuchBucket" : "Bucket '%s' does not exist",
        "AccessDenied" : "Access to bucket '%s' was denied",
        "BucketAlreadyExists" : "Bucket '%s' already exists",
        }

    error_codes = {
        "SIZE_MISMATCH":1,
        "MD5_MISMATCH":2,
        "RETRIES_EXCEEDED":3,
        "UPLOAD_ABORT":4,
        "MD5_META_NOTFOUND":5,
        "KEYBOARD_INTERRUPT":6
        }


    ## S3 sometimes sends HTTP-307 response
    redir_map = {}

    ## Maximum attempts of re-issuing failed requests
    _max_retries = 5

    ##Default exit status = 0 (SUCCESS)
    exit_status = 0

    def __init__(self, config):
        self.config = config

    def get_connection(self, bucket):
        if self.config.proxy_host != "":
            return httplib.HTTPConnection(self.config.proxy_host, self.config.proxy_port)
        else:
            if self.config.use_https:
                return httplib.HTTPSConnection(self.get_hostname(bucket))
            else:
                return httplib.HTTPConnection(self.get_hostname(bucket))

    def get_hostname(self, bucket):
        if bucket and check_bucket_name_dns_conformity(bucket):
            if self.redir_map.has_key(bucket):
                host = self.redir_map[bucket]
            else:
                host = getHostnameFromBucket(bucket)
        else:
            host = self.config.host_base
        debug('get_hostname(%s): %s' % (bucket, host))
        return host

    def set_hostname(self, bucket, redir_hostname):
        self.redir_map[bucket] = redir_hostname

    def format_uri(self, resource):
        if resource['bucket'] and not check_bucket_name_dns_conformity(resource['bucket']):
            uri = "/%s%s" % (resource['bucket'], resource['uri'])
        else:
            uri = resource['uri']
        if self.config.proxy_host != "":
            uri = "http://%s%s" % (self.get_hostname(resource['bucket']), uri)
        debug('format_uri(): ' + uri)
        return uri

    ## Commands / Actions
    def list_all_buckets(self):
        request = self.create_request("LIST_ALL_BUCKETS")
        response = self.send_request(request)
        response["list"] = getListFromXml(response["data"], "Bucket")
        return response

    def bucket_list(self, bucket, prefix = None, recursive = None):
        def _list_truncated(data):
            ## <IsTruncated> can either be "true" or "false" or be missing completely
            is_truncated = getTextFromXml(data, ".//IsTruncated") or "false"
            return is_truncated.lower() != "false"

        def _get_contents(data):
            return getListFromXml(data, "Contents")

        def _get_common_prefixes(data):
            return getListFromXml(data, "CommonPrefixes")

        uri_params = {}
        truncated = True
        list = []
        prefixes = []

        while truncated:
            response = self.bucket_list_noparse(bucket, prefix, recursive, uri_params)
            current_list = _get_contents(response["data"])
            current_prefixes = _get_common_prefixes(response["data"])
            truncated = _list_truncated(response["data"])
            if truncated:
                if current_list:
                    uri_params['marker'] = self.urlencode_string(current_list[-1]["Key"])
                else:
                    uri_params['marker'] = self.urlencode_string(current_prefixes[-1]["Prefix"])
                debug("Listing continues after '%s'" % uri_params['marker'])

            list += current_list
            prefixes += current_prefixes

        response['list'] = list
        response['common_prefixes'] = prefixes
        return response

    def bucket_list_noparse(self, bucket, prefix = None, recursive = None, uri_params = {}):
        if prefix:
            uri_params['prefix'] = self.urlencode_string(prefix)
        if not self.config.recursive and not recursive:
            uri_params['delimiter'] = "/"
        request = self.create_request("BUCKET_LIST", bucket = bucket, **uri_params)
        response = self.send_request(request)
        #debug(response)
        return response

    def bucket_create(self, bucket, bucket_location = None):
        headers = SortedDict(ignore_case = True)
        body = ""
        if bucket_location and bucket_location.strip().upper() != "US":
            bucket_location = bucket_location.strip()
            if bucket_location.upper() == "EU":
                bucket_location = bucket_location.upper()
            else:
                bucket_location = bucket_location.lower()
            body  = "<CreateBucketConfiguration><LocationConstraint>"
            body += bucket_location
            body += "</LocationConstraint></CreateBucketConfiguration>"
            debug("bucket_location: " + body)
            check_bucket_name(bucket, dns_strict = True)
        else:
            check_bucket_name(bucket, dns_strict = False)
        if self.config.acl_public:
            headers["x-amz-acl"] = "public-read"
        request = self.create_request("BUCKET_CREATE", bucket = bucket, headers = headers)
        response = self.send_request(request, body)
        return response

    def bucket_delete(self, bucket):
        request = self.create_request("BUCKET_DELETE", bucket = bucket)
        response = self.send_request(request)
        return response

    def get_bucket_location(self, uri):
        request = self.create_request("BUCKET_LIST", bucket = uri.bucket(), extra = "?location")
        response = self.send_request(request)
        location = getTextFromXml(response['data'], "LocationConstraint")
        if not location or location in [ "", "US" ]:
            location = "us-east-1"
        elif location == "EU":
            location = "eu-west-1"
        return location

    def bucket_info(self, uri):
        # For now reports only "Location". One day perhaps more.
        response = {}
        response['bucket-location'] = self.get_bucket_location(uri)
        return response

    def website_info(self, uri, bucket_location = None):
        headers = SortedDict(ignore_case = True)
        bucket = uri.bucket()
        body = ""

        request = self.create_request("BUCKET_LIST", bucket = bucket, extra="?website")
        try:
            response = self.send_request(request, body)
            response['index_document'] = getTextFromXml(response['data'], ".//IndexDocument//Suffix")
            response['error_document'] = getTextFromXml(response['data'], ".//ErrorDocument//Key")
            response['website_endpoint'] = self.config.website_endpoint % {
                "bucket" : uri.bucket(),
                "location" : self.get_bucket_location(uri)}
            return response
        except S3Error, e:
            if e.status == 404:
                debug("Could not get /?website - website probably not configured for this bucket")
                return None
            raise

    def website_create(self, uri, bucket_location = None):
        headers = SortedDict(ignore_case = True)
        bucket = uri.bucket()
        body = '<WebsiteConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        body += '  <IndexDocument>'
        body += ('    <Suffix>%s</Suffix>' % self.config.website_index)
        body += '  </IndexDocument>'
        if self.config.website_error:
            body += '  <ErrorDocument>'
            body += ('    <Key>%s</Key>' % self.config.website_error)
            body += '  </ErrorDocument>'
        body += '</WebsiteConfiguration>'

        request = self.create_request("BUCKET_CREATE", bucket = bucket, extra="?website")
        debug("About to send request '%s' with body '%s'" % (request, body))
        response = self.send_request(request, body)
        debug("Received response '%s'" % (response))

        return response

    def website_delete(self, uri, bucket_location = None):
        headers = SortedDict(ignore_case = True)
        bucket = uri.bucket()
        body = ""

        request = self.create_request("BUCKET_DELETE", bucket = bucket, extra="?website")
        debug("About to send request '%s' with body '%s'" % (request, body))
        response = self.send_request(request, body)
        debug("Received response '%s'" % (response))

        if response['status'] != 204:
            raise S3ResponseError("Expected status 204: %s" % response)

        return response

    def object_multipart_upload(self, filename, uri, cfg, extra_headers = None, extra_label = ""):
        if uri.type != "s3":
            raise ValueError("Expected URI type 's3', got '%s'" % uri.type)

        if not os.path.isfile(filename):
            raise InvalidFileError(u"%s is not a regular file" % unicodise(filename))
        try:
            file = open(filename, "rb")
            file_size = os.stat(filename)[ST_SIZE]
        except (IOError, OSError), e:
            raise InvalidFileError(u"%s: %s" % (unicodise(filename), e.strerror))

        parts_size = file_size / cfg.parallel_multipart_upload_count
        debug("File size=%d parts size=%d" %(file_size, parts_size))
        if parts_size < 5*1024*1024:
            warning("File part size is less than minimum required size (5 MB). Disabled parallel multipart upload")
            return self.object_put(filename, uri, extra_headers = extra_headers, extra_label = extra_label)

        md5_hash = hash_file_md5(filename)
        info("Calculating md5sum for %s" %filename)
        headers = SortedDict(ignore_case = True)
        if extra_headers:
            headers.update(extra_headers)

        content_type = self.config.mime_type
        if not content_type and self.config.guess_mime_type:
            content_type = mimetypes.guess_type(filename)[0]
        if not content_type:
            content_type = self.config.default_mime_type
        debug("Content-Type set to '%s'" % content_type)
        headers["content-type"] = content_type
        if self.config.acl_public:
            headers["x-amz-acl"] = "public-read"
        if self.config.reduced_redundancy:
            headers["x-amz-storage-class"] = "REDUCED_REDUNDANCY"

        headers = {}
        headers['date'] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        headers['x-amz-meta-md5sum'] = md5_hash
        initiate_request = self.create_request("OBJECT_POST", headers = headers, uri = uri, uploads='')
        initiate_response = self.send_request(initiate_request)
        upload_id = getTextFromXml(initiate_response["data"], ".//UploadId")
        #Upload single file size

        debug("Upload ID = %s" %upload_id)

        multipart_ranges = []
        global upload_worker_queue
        global part_upload_list

        part_upload_list = {}
        upload_worker_queue = Queue.Queue()
        i = 1
        for offset in range(0, file_size, parts_size):
            start_offset = offset 
         
            if start_offset + parts_size - 1 < file_size:
                end_offset = start_offset + parts_size - 1
                if i == cfg.parallel_multipart_upload_count:
                    end_offset = file_size - 1
            else:
                end_offset = file_size - 1  

            item = {'part_no':i, 'start_position':start_offset, 'end_position':end_offset, 'uri':uri, 'upload_id':upload_id, 'filename':filename}

            multipart_ranges.append(item)
            upload_worker_queue.put(item)
            debug("Part %d start=%d end=%d (part size=%d)" %(i, start_offset, end_offset, parts_size))
            i+=1
            if end_offset == file_size - 1:
                break

        def part_upload_worker():
            while True:
                try:
                    part_info = upload_worker_queue.get_nowait()
                except Queue.Empty:
                    return
                part_number = part_info['part_no']
                start_position = part_info['start_position']
                end_position = part_info['end_position']
                uri = part_info['uri']
                upload_id = part_info['upload_id']
                filename = part_info['filename']

                file = open(filename, 'rb')

                headers = SortedDict(ignore_case = True)
                #if extra_headers:
                #    headers.update(extra_headers)

                headers["content-length"] = end_position - start_position + 1
                headers['Expect'] = '100-continue'

                request = self.create_request("OBJECT_PUT", uri = uri, headers = headers, partNumber = part_number, uploadId = upload_id)
                labels = { 'source' : unicodise(filename), 'destination' : unicodise(uri.uri()), 'extra' : extra_label }
                try:
                    response = self.send_file(request, file, labels, retries = self._max_retries, part_info = part_info)
                except S3UploadError, e:
                    self.abort_multipart_upload(uri, upload_id)
                    file.close()
                    raise S3UploadError("Failed to upload part-%d to S3" %part_info['part_no'])
                part_upload_list[part_number] = response["headers"]["etag"].strip('"\'')
                file.close()

        for i in range(cfg.parallel_multipart_upload_threads):
            t = threading.Thread(target=part_upload_worker)
            t.setDaemon(True)
            t.start()

        timestamp_start = time.time()
        while threading.activeCount() > 1:
            time.sleep(0.1)
        debug("Upload of file parts complete")

        body = "<CompleteMultipartUpload>\n"
        for part in part_upload_list.keys():
            body += "  <Part>\n"
            body += "   <PartNumber>%d</PartNumber>\n" %part
            body += "   <ETag>%s</ETag>\n" %part_upload_list[part]
            body += "  </Part>\n"
        body += "</CompleteMultipartUpload>"

        complete_request = self.create_request("OBJECT_POST", uri = uri, uploadId = upload_id)
        response = self.send_request(complete_request, body)
        timestamp_end = time.time()

        object_info = self.object_info(uri)
        upload_size = int(object_info['headers']['content-length'])
        #file_md5sum = info['headers']['etag'].strip('"')

        response = {}
        response["headers"] = object_info["headers"]
        #response["md5match"] = file_md5sum.strip() == md5_hash_file.strip()
        response["md5"] = md5_hash
        #if not response["md5match"]:
        #    warning("MD5 signatures do not match: computed=%s, received=%s" % (md5_hash_file, file_md5sum))
        #    warning("Aborting file upload")
        #    self.abort_multipart_upload(uri, upload_id)
        #    raise S3UploadError

        response["elapsed"] = timestamp_end - timestamp_start
        response["size"] = file_size
        response["speed"] = response["elapsed"] and float(response["size"]) / response["elapsed"] or float(-1)
        if response["size"] != upload_size:
            warning("Reported size (%s) does not match received size (%s)" % (upload_size, response["size"]))
            self.abort_multipart_upload()
        return response


    def object_put(self, filename, uri, extra_headers = None, extra_label = ""):
        # TODO TODO
        # Make it consistent with stream-oriented object_get()
        if uri.type != "s3":
            raise ValueError("Expected URI type 's3', got '%s'" % uri.type)

        if not os.path.isfile(filename):
            raise InvalidFileError(u"%s is not a regular file" % unicodise(filename))
        try:
            file = open(filename, "rb")
            size = os.stat(filename)[ST_SIZE]
        except (IOError, OSError), e:
            raise InvalidFileError(u"%s: %s" % (unicodise(filename), e.strerror))
        headers = SortedDict(ignore_case = True)
        if extra_headers:
            headers.update(extra_headers)
        headers["content-length"] = size
        content_type = self.config.mime_type
        if not content_type and self.config.guess_mime_type:
            content_type = mimetypes.guess_type(filename)[0]
        if not content_type:
            content_type = self.config.default_mime_type
        debug("Content-Type set to '%s'" % content_type)
        headers["content-type"] = content_type
        if self.config.acl_public:
            headers["x-amz-acl"] = "public-read"
        if self.config.reduced_redundancy:
            headers["x-amz-storage-class"] = "REDUCED_REDUNDANCY"
        request = self.create_request("OBJECT_PUT", uri = uri, headers = headers)
        labels = { 'source' : unicodise(filename), 'destination' : unicodise(uri.uri()), 'extra' : extra_label }
        response = self.send_file(request, file, labels, retries = self._max_retries)
        return response

    def object_get(self, uri, stream, start_position = 0, extra_label = ""):
        if uri.type != "s3":
            raise ValueError("Expected URI type 's3', got '%s'" % uri.type)
        request = self.create_request("OBJECT_GET", uri = uri)
        labels = { 'source' : unicodise(uri.uri()), 'destination' : unicodise(stream.name), 'extra' : extra_label }
        response = self.recv_file(request, stream, labels, start_position)
        return response

    def object_multipart_get(self, uri, stream, cfg, start_position = 0, extra_label = ""):
        debug("Executing multipart download")
        if uri.type != "s3":
            raise ValueError("Expected URI type 's3', got '%s'" % uri.type)
        object_info = self.object_info(uri)
        file_size = int(object_info['headers']['content-length'])
        file_md5sum = object_info['headers']['etag'].strip('"')
        if len(file_md5sum.split('-')) == 2:
            try:
                file_md5sum = object_info['headers']['x-amz-meta-md5sum']
            except:
                warning('md5sum meta information not found in multipart uploaded file')

        multipart_ranges = []
        parts_size = file_size / cfg.parallel_multipart_download_count 
        global worker_queue
        tmp_dir = os.path.join(os.path.dirname(stream.name),'tmps3')
        os.makedirs(tmp_dir)

        worker_queue = Queue.Queue()
        i = 1
        for offset in range(0, file_size, parts_size):
            start_offset = offset 
            if start_offset + parts_size - 1 < file_size:
                end_offset = start_offset + parts_size - 1
                if i == cfg.parallel_multipart_download_count:
                    end_offset = file_size - 1
            else:
                end_offset = file_size - 1  

            part_stream = open(os.path.join(tmp_dir, "%s.part-%d" %(os.path.basename(stream.name), i)),'wb+')
            item = (i, start_offset, end_offset, uri, part_stream)

            multipart_ranges.append(item)
            worker_queue.put(item)
            i+=1

            if end_offset == file_size - 1:
                break


        def get_worker():
            while True:
                try:
                    item = worker_queue.get_nowait()
                except Queue.Empty:
                    return
                offset = item[0]
                start_position = item[1]
                end_position = item[2]
                uri = item[3]
                stream = item[4]
                request = self.create_request("OBJECT_GET", uri = uri)
                labels = { 'source' : unicodise(uri.uri()), 'destination' : unicodise(stream.name), 'extra' : extra_label }
                self.recv_file(request, stream, labels, start_position, retries = self._max_retries, end_position = end_position)

        for i in range(cfg.parallel_multipart_download_threads):
            t = threading.Thread(target=get_worker)
            t.setDaemon(True)
            t.start()

        timestamp_start = time.time()
        while threading.activeCount() > 1:
            time.sleep(0.1)
        debug("Download of file parts complete")
        source_streams = map(lambda x: x[4], multipart_ranges)
        md5_hash_download, download_size = concat_files(stream, True, *source_streams)
        timestamp_end = time.time()
        os.rmdir(tmp_dir)
        stream.flush()

        debug("ReceivedFile: Computed MD5 = %s" % md5_hash_download)
        response = {}
        response["headers"] = object_info["headers"]
        response["md5match"] =  file_md5sum.strip() == md5_hash_download.strip()
        response["md5"] = file_md5sum 
        if not response["md5match"]:
            warning("MD5 signatures do not match: computed=%s, received=%s" % (md5_hash_download, file_md5sum))
            self.exit_status = self.error_codes["MD5_MISMATCH"]

        response["elapsed"] = timestamp_end - timestamp_start
        response["size"] = file_size
        response["speed"] = response["elapsed"] and float(response["size"]) / response["elapsed"] or float(-1)
        if response["size"] != download_size:
            warning("Reported size (%s) does not match received size (%s)" % (download_size, response["size"]))
            self.exit_status = self.error_codes["SIZE_MISMATCH"]
        return response 

    def object_delete(self, uri):
        if uri.type != "s3":
            raise ValueError("Expected URI type 's3', got '%s'" % uri.type)
        request = self.create_request("OBJECT_DELETE", uri = uri)
        response = self.send_request(request)
        return response

    def object_copy(self, src_uri, dst_uri, extra_headers = None):
        if src_uri.type != "s3":
            raise ValueError("Expected URI type 's3', got '%s'" % src_uri.type)
        if dst_uri.type != "s3":
            raise ValueError("Expected URI type 's3', got '%s'" % dst_uri.type)
        headers = SortedDict(ignore_case = True)
        headers['x-amz-copy-source'] = "/%s/%s" % (src_uri.bucket(), self.urlencode_string(src_uri.object()))
        ## TODO: For now COPY, later maybe add a switch?
        headers['x-amz-metadata-directive'] = "COPY"
        if self.config.acl_public:
            headers["x-amz-acl"] = "public-read"
        if self.config.reduced_redundancy:
            headers["x-amz-storage-class"] = "REDUCED_REDUNDANCY"
        # if extra_headers:
        #   headers.update(extra_headers)
        request = self.create_request("OBJECT_PUT", uri = dst_uri, headers = headers)
        response = self.send_request(request)
        return response

    def object_move(self, src_uri, dst_uri, extra_headers = None):
        response_copy = self.object_copy(src_uri, dst_uri, extra_headers)
        debug("Object %s copied to %s" % (src_uri, dst_uri))
        if getRootTagName(response_copy["data"]) == "CopyObjectResult":
            response_delete = self.object_delete(src_uri)
            debug("Object %s deleted" % src_uri)
        return response_copy

    def object_info(self, uri):
        request = self.create_request("OBJECT_HEAD", uri = uri)
        response = self.send_request(request)
        return response

    def get_acl(self, uri):
        if uri.has_object():
            request = self.create_request("OBJECT_GET", uri = uri, extra = "?acl")
        else:
            request = self.create_request("BUCKET_LIST", bucket = uri.bucket(), extra = "?acl")

        response = self.send_request(request)
        acl = ACL(response['data'])
        return acl

    def set_acl(self, uri, acl):
        if uri.has_object():
            request = self.create_request("OBJECT_PUT", uri = uri, extra = "?acl")
        else:
            request = self.create_request("BUCKET_CREATE", bucket = uri.bucket(), extra = "?acl")

        body = str(acl)
        debug(u"set_acl(%s): acl-xml: %s" % (uri, body))
        response = self.send_request(request, body)
        return response

    def get_accesslog(self, uri):
        request = self.create_request("BUCKET_LIST", bucket = uri.bucket(), extra = "?logging")
        response = self.send_request(request)
        accesslog = AccessLog(response['data'])
        return accesslog

    def set_accesslog_acl(self, uri):
        acl = self.get_acl(uri)
        debug("Current ACL(%s): %s" % (uri.uri(), str(acl)))
        acl.appendGrantee(GranteeLogDelivery("READ_ACP"))
        acl.appendGrantee(GranteeLogDelivery("WRITE"))
        debug("Updated ACL(%s): %s" % (uri.uri(), str(acl)))
        self.set_acl(uri, acl)

    def set_accesslog(self, uri, enable, log_target_prefix_uri = None, acl_public = False):
        request = self.create_request("BUCKET_CREATE", bucket = uri.bucket(), extra = "?logging")
        accesslog = AccessLog()
        if enable:
            accesslog.enableLogging(log_target_prefix_uri)
            accesslog.setAclPublic(acl_public)
        else:
            accesslog.disableLogging()
        body = str(accesslog)
        debug(u"set_accesslog(%s): accesslog-xml: %s" % (uri, body))
        try:
            response = self.send_request(request, body)
        except S3Error, e:
            if e.info['Code'] == "InvalidTargetBucketForLogging":
                info("Setting up log-delivery ACL for target bucket.")
                self.set_accesslog_acl(S3Uri("s3://%s" % log_target_prefix_uri.bucket()))
                response = self.send_request(request, body)
            else:
                raise
        return accesslog, response

    ## Low level methods
    def urlencode_string(self, string, urlencoding_mode = None):
        if type(string) == unicode:
            string = string.encode("utf-8")

        if urlencoding_mode is None:
            urlencoding_mode = self.config.urlencoding_mode

        if urlencoding_mode == "verbatim":
            ## Don't do any pre-processing
            return string

        encoded = ""
        ## List of characters that must be escaped for S3
        ## Haven't found this in any official docs
        ## but my tests show it's more less correct.
        ## If you start getting InvalidSignature errors
        ## from S3 check the error headers returned
        ## from S3 to see whether the list hasn't
        ## changed.
        for c in string:    # I'm not sure how to know in what encoding
                    # 'object' is. Apparently "type(object)==str"
                    # but the contents is a string of unicode
                    # bytes, e.g. '\xc4\x8d\xc5\xafr\xc3\xa1k'
                    # Don't know what it will do on non-utf8
                    # systems.
                    #           [hope that sounds reassuring ;-)]
            o = ord(c)
            if (o < 0x20 or o == 0x7f):
                if urlencoding_mode == "fixbucket":
                    encoded += "%%%02X" % o
                else:
                    error(u"Non-printable character 0x%02x in: %s" % (o, string))
                    error(u"Please report it to s3tools-bugs@lists.sourceforge.net")
                    encoded += replace_nonprintables(c)
            elif (o == 0x20 or  # Space and below
                o == 0x22 or    # "
                o == 0x23 or    # #
                o == 0x25 or    # % (escape character)
                o == 0x26 or    # &
                o == 0x2B or    # + (or it would become <space>)
                o == 0x3C or    # <
                o == 0x3E or    # >
                o == 0x3F or    # ?
                o == 0x60 or    # `
                o >= 123):      # { and above, including >= 128 for UTF-8
                encoded += "%%%02X" % o
            else:
                encoded += c
        debug("String '%s' encoded to '%s'" % (string, encoded))
        return encoded

    def create_request(self, operation, uri = None, bucket = None, object = None, headers = None, extra = None, **params):
        resource = { 'bucket' : None, 'uri' : "/" }

        if uri and (bucket or object):
            raise ValueError("Both 'uri' and either 'bucket' or 'object' parameters supplied")
        ## If URI is given use that instead of bucket/object parameters
        if uri:
            bucket = uri.bucket()
            object = uri.has_object() and uri.object() or None

        if bucket:
            resource['bucket'] = str(bucket)
            if object:
                resource['uri'] = "/" + self.urlencode_string(object)
        if extra:
            resource['uri'] += extra

        method_string = S3.http_methods.getkey(S3.operations[operation] & S3.http_methods["MASK"])

        request = S3Request(self, method_string, resource, headers, params)

        debug("CreateRequest: resource[uri]=" + resource['uri'])
        debug("Request: headers="+str(headers))
        return request

    def _fail_wait(self, retries):
        # Wait a few seconds. The more it fails the more we wait.
        return (self._max_retries - retries + 1) * 3

    def send_request(self, request, body = None, retries = _max_retries):
        method_string, resource, headers = request.get_triplet()
        debug("Processing request, please wait...")
        if not headers.has_key('content-length'):
            headers['content-length'] = body and len(body) or 0
        try:
            # "Stringify" all headers
            for header in headers.keys():
                headers[header] = str(headers[header])
            conn = self.get_connection(resource['bucket'])
            debug("Sending Request: method:%s body: %s uri: %s headers:%s" %(method_string,body,self.format_uri(resource),str(headers)))
            conn.request(method_string, self.format_uri(resource), body, headers)
            response = {}
            http_response = conn.getresponse()
            response["status"] = http_response.status
            response["reason"] = http_response.reason
            response["headers"] = convertTupleListToDict(http_response.getheaders())
            response["data"] =  http_response.read()
            debug("Response: " + str(response))
            conn.close()
        except Exception, e:
            if retries:
                warning("Retrying failed request: %s (%s)" % (resource['uri'], e))
                warning("Waiting %d sec..." % self._fail_wait(retries))
                time.sleep(self._fail_wait(retries))
                return self.send_request(request, body, retries - 1)
            else:
                raise S3RequestError("Request failed for: %s" % resource['uri'])

        if response["status"] == 307:
            ## RedirectPermanent
            redir_bucket = getTextFromXml(response['data'], ".//Bucket")
            redir_hostname = getTextFromXml(response['data'], ".//Endpoint")
            self.set_hostname(redir_bucket, redir_hostname)
            warning("Redirected to: %s" % (redir_hostname))
            return self.send_request(request, body)

        if response["status"] >= 500:
            e = S3Error(response)
            if retries:
                warning(u"Retrying failed request: %s" % resource['uri'])
                warning(unicode(e))
                warning("Waiting %d sec..." % self._fail_wait(retries))
                time.sleep(self._fail_wait(retries))
                return self.send_request(request, body, retries - 1)
            else:
                raise e

        if response["status"] < 200 or response["status"] > 299:
            raise S3Error(response)

        return response

    def abort_multipart_upload(self, uri, upload_id):
        headers = {}
        headers['date'] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        request = self.create_request("OBJECT_DELETE", headers = headers, uri = uri, UploadId = upload_id)
        response = self.send_request(request)
        self.exit_status = self.error_codes["UPLOAD_ABORT"]
        return response 

    def send_file(self, request, file, labels, throttle = 0, retries = _max_retries, part_info = None):
        method_string, resource, headers = request.get_triplet()
        size_left = size_total = headers.get("content-length")
        if self.config.progress_meter:
            progress = self.config.progress_class(labels, size_total)
        else:
            if part_info:
                info("Sending file '%s' part-%d, please wait..." % (file.name, part_info['part_no']))
            else:
                info("Sending file '%s', please wait..." % file.name)

        timestamp_start = time.time()
        try:
            conn = self.get_connection(resource['bucket'])
            conn.connect()
            conn.putrequest(method_string, self.format_uri(resource))
            for header in headers.keys():
                conn.putheader(header, str(headers[header]))
            conn.endheaders()
        except Exception, e:
            if self.config.progress_meter:
                progress.done("failed")
            if retries:
                warning("Retrying failed request: %s (%s)" % (resource['uri'], e))
                warning("Waiting %d sec..." % self._fail_wait(retries))
                time.sleep(self._fail_wait(retries))
                # Connection error -> same throttle value
                return self.send_file(request, file, labels, throttle, retries - 1, part_info)
            else:
                self.exit_status = self.error_codes["RETRIES_EXCEEDED"]
                raise S3UploadError("Upload failed for: %s" % resource['uri'])

        if part_info:
            file.seek(part_info['start_position'])
        else:
            file.seek(0)

        md5_hash = md5()
        try:
            while (size_left > 0):
                #debug("SendFile: Reading up to %d bytes from '%s'" % (self.config.send_chunk, file.name))
                if size_left < self.config.send_chunk:
                    chunk_size = size_left
                else:
                    chunk_size = self.config.send_chunk
                data = file.read(chunk_size)
                md5_hash.update(data)
                conn.send(data)
                if self.config.progress_meter:
                    progress.update(delta_position = len(data))
                size_left -= len(data)
                if throttle:
                    time.sleep(throttle)
            md5_computed = md5_hash.hexdigest()
            response = {}
            http_response = conn.getresponse()
            response["status"] = http_response.status
            response["reason"] = http_response.reason
            response["headers"] = convertTupleListToDict(http_response.getheaders())
            response["data"] = http_response.read()
            response["size"] = size_total
            conn.close()
            debug(u"Response: %s" % response)
        except Exception, e:
            if self.config.progress_meter:
                progress.done("failed")
            debug("Retries:"+str(retries))
            if retries:
                if retries < self._max_retries:
                    throttle = throttle and throttle * 5 or 0.01
                warning("Upload failed: %s (%s)" % (resource['uri'], e))
                warning("Retrying on lower speed (throttle=%0.2f)" % throttle)
                warning("Waiting %d sec..." % self._fail_wait(retries))
                time.sleep(self._fail_wait(retries))
                # Connection error -> same throttle value
                return self.send_file(request, file, labels, throttle, retries - 1, part_info)
            else:
                debug("Giving up on '%s' %s" % (file.name, e))
                self.exit_status = self.error_codes["RETRIES_EXCEEDED"]
                raise S3UploadError("Upload failed for: %s" % resource['uri'])

        timestamp_end = time.time()
        response["elapsed"] = timestamp_end - timestamp_start
        response["speed"] = response["elapsed"] and float(response["size"]) / response["elapsed"] or float(-1)

        if self.config.progress_meter:
            ## The above conn.close() takes some time -> update() progress meter
            ## to correct the average speed. Otherwise people will complain that
            ## 'progress' and response["speed"] are inconsistent ;-)
            progress.update()
            progress.done("done")

        if response["status"] == 307:
            ## RedirectPermanent
            redir_bucket = getTextFromXml(response['data'], ".//Bucket")
            redir_hostname = getTextFromXml(response['data'], ".//Endpoint")
            self.set_hostname(redir_bucket, redir_hostname)
            warning("Redirected to: %s" % (redir_hostname))
            return self.send_file(request, file, labels, retries, part_info)

        # S3 from time to time doesn't send ETag back in a response :-(
        # Force re-upload here.
        if not response['headers'].has_key('etag'):
            response['headers']['etag'] = ''

        if response["status"] < 200 or response["status"] > 299:
            try_retry = False
            if response["status"] >= 500:
                ## AWS internal error - retry
                try_retry = True
            elif response["status"] >= 400:
                err = S3Error(response)
                ## Retriable client error?
                if err.code in [ 'BadDigest', 'OperationAborted', 'TokenRefreshRequired', 'RequestTimeout' ]:
                    try_retry = True

            if try_retry:
                if retries:
                    warning("Upload failed: %s (%s)" % (resource['uri'], S3Error(response)))
                    warning("Waiting %d sec..." % self._fail_wait(retries))
                    time.sleep(self._fail_wait(retries))
                    return self.send_file(request, file, labels, throttle, retries - 1, part_info)
                else:
                    warning("Too many failures. Giving up on '%s'" % (file.name))
                    self.exit_status = self.error_codes["RETRIES_EXCEEDED"]
                    raise S3UploadError

            ## Non-recoverable error
            raise S3Error(response)

        debug("MD5 sums: computed=%s, received=%s" % (md5_computed, response["headers"]["etag"]))
        if response["headers"]["etag"].strip('"\'') != md5_hash.hexdigest():
            warning("MD5 Sums don't match!")
            self.exit_status = self.error_codes["MD5_MISMATCH"]
            if retries:
                warning("Retrying upload of %s" % (file.name))
                return self.send_file(request, file, labels, throttle, retries - 1, part_info)
            else:
                warning("Too many failures. Giving up on '%s'" % (file.name))
                self.exit_status = self.error_codes["RETRIES_EXCEEDED"]
                raise S3UploadError

        return response

    def recv_file(self, request, stream, labels, start_position = 0, retries = _max_retries, end_position = -1):
        method_string, resource, headers = request.get_triplet()
        if self.config.progress_meter:
            progress = self.config.progress_class(labels, 0)
        else:
            info("Receiving file '%s', please wait..." % stream.name)
        stream.seek(0)
        timestamp_start = time.time()
        try:
            conn = self.get_connection(resource['bucket'])
            conn.connect()
            conn.putrequest(method_string, self.format_uri(resource))
            for header in headers.keys():
                conn.putheader(header, str(headers[header]))
            if start_position > 0 and end_position == -1:
                debug("Requesting Range: %d .. end" % start_position)
                conn.putheader("Range", "bytes=%d-" % start_position)
            elif end_position != -1:
                debug("Requesting Range: %d .. %d" % (start_position, end_position))
                conn.putheader("Range", "bytes=%d-%d" % (start_position, end_position))
            conn.endheaders()
            response = {}
            http_response = conn.getresponse()
            response["status"] = http_response.status
            response["reason"] = http_response.reason
            response["headers"] = convertTupleListToDict(http_response.getheaders())
            debug("Response: %s" % response)
        except Exception, e:
            if self.config.progress_meter:
                progress.done("failed")
            if retries:
                warning("Retrying failed request: %s (%s)" % (resource['uri'], e))
                warning("Waiting %d sec..." % self._fail_wait(retries))
                time.sleep(self._fail_wait(retries))
                # Connection error -> same throttle value
                return self.recv_file(request, stream, labels, start_position, retries - 1, end_position)
            else:
                self.exit_status = self.error_codes["RETRIES_EXCEEDED"]
                raise S3DownloadError("Download failed for: %s" % resource['uri'])

        if response["status"] == 307:
            ## RedirectPermanent
            response['data'] = http_response.read()
            redir_bucket = getTextFromXml(response['data'], ".//Bucket")
            redir_hostname = getTextFromXml(response['data'], ".//Endpoint")
            self.set_hostname(redir_bucket, redir_hostname)
            warning("Redirected to: %s" % (redir_hostname))
            return self.recv_file(request, stream, labels, start_position, retries, end_position)

        if response["status"] < 200 or response["status"] > 299:
            raise S3Error(response)

        if start_position == 0 and end_position == -1:
            # Only compute MD5 on the fly if we're downloading from beginning
            # Otherwise we'd get a nonsense.
            md5_hash = md5()
        size_left = int(response["headers"]["content-length"])
        size_total = start_position + size_left
        current_position = start_position

        if self.config.progress_meter:
            progress.total_size = size_total
            progress.initial_position = current_position
            progress.current_position = current_position

        try:
            while (current_position < size_total):
                this_chunk = size_left > self.config.recv_chunk and self.config.recv_chunk or size_left
                data = http_response.read(this_chunk)
                stream.write(data)
                if start_position == 0 and end_position == -1:
                    md5_hash.update(data)
                current_position += len(data)
                ## Call progress meter from here...
                if self.config.progress_meter:
                    progress.update(delta_position = len(data))
            conn.close()
        except Exception, e:
            if self.config.progress_meter:
                progress.done("failed")
            if retries:
                warning("Retrying failed request: %s (%s)" % (resource['uri'], e))
                warning("Waiting %d sec..." % self._fail_wait(retries))
                time.sleep(self._fail_wait(retries))
                # Connection error -> same throttle value
                if end_position != -1:
                   return self.recv_file(request, stream, labels, current_position, retries - 1)
                else:
                   return self.recv_file(request, stream, labels, start_position, retries - 1, end_position)
            else:
                self.exit_status = self.error_codes["RETRIES_EXCEEDED"]
                raise S3DownloadError("Download failed for: %s" % resource['uri'])

        stream.flush()
        timestamp_end = time.time()

        if self.config.progress_meter:
            ## The above stream.flush() may take some time -> update() progress meter
            ## to correct the average speed. Otherwise people will complain that
            ## 'progress' and response["speed"] are inconsistent ;-)
            progress.update()
            progress.done("done")

        if end_position == -1:
            if start_position == 0:
                # Only compute MD5 on the fly if we were downloading from the beginning
                response["md5"] = md5_hash.hexdigest()
            else:
                # Otherwise try to compute MD5 of the output file
                try:
                    response["md5"] = hash_file_md5(stream.name)
                except IOError, e:
                    if e.errno != errno.ENOENT:
                        warning("Unable to open file: %s: %s" % (stream.name, e))
                    warning("Unable to verify MD5. Assume it matches.")
                    response["md5"] = response["headers"]["etag"]

            file_md5sum = response["headers"]["etag"].strip('"\'')
            if len(response["headers"]["etag"].split('-')) == 2:
                try:
                    file_md5sum = response['headers']['x-amz-meta-md5sum']
                except:
                    warning('md5sum meta information not found in multipart uploaded file')
                    self.exit_status = self.error_codes["MD5_META_NOTFOUND"]

            response["md5match"] = file_md5sum == response["md5"]
            debug("ReceiveFile: Computed MD5 = %s" % response["md5"])
            if not response["md5match"]:
                warning("MD5 signatures do not match: computed=%s, received=%s" % (
                    response["md5"], response["headers"]["etag"]))
                self.exit_status = self.error_codes["MD5_MISMATCH"]
        response["elapsed"] = timestamp_end - timestamp_start
        response["size"] = current_position
        response["speed"] = response["elapsed"] and float(response["size"]) / response["elapsed"] or float(-1)
        if response["size"] != start_position + long(response["headers"]["content-length"]):
            warning("Reported size (%s) does not match received size (%s)" % (
                start_position + response["headers"]["content-length"], response["size"]))
            self.exit_status = self.error_codes["SIZE_MISMATCH"]
        return response
__all__.append("S3")

# vim:et:ts=4:sts=4:ai
