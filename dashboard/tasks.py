# !/usr/bin/env python
# -*- coding: utf-8 -*-
# created by restran on 2016/1/2

from __future__ import unicode_literals, absolute_import

import traceback

from pymongo import UpdateOne
from future.utils import iteritems
from .models import *
from api_dashboard import settings
from api_dashboard.celery import app
from .utils import RedisHelper
import json
import logging

logger = logging.getLogger(__name__)


def id_to_str(entity_id):
    return text_type(entity_id) if entity_id is not None else ''


def do_clear_old_access_logs(expires, limit=1000):
    logs = AccessLog.objects(accessed_at__lt=expires).only(
        'id', 'request.headers', 'request.body',
        'response.headers', 'response.body')[:limit]
    logs = list(logs)

    request_headers_dict = {}
    request_body_dict = {}
    response_headers_dict = {}
    response_body_dict = {}

    def count_id(obj, container):
        _id = obj.grid_id
        if _id is None:
            pass
        else:
            if _id not in container:
                container[_id] = 1
            else:
                container[_id] += 1

    # 找出非重复的 headers 和 body 对应的文件 id
    for t in logs:
        map(count_id, [t.request.headers, t.request.body,
                       t.response.headers, t.request.body],
            [request_headers_dict, request_body_dict,
             response_headers_dict, response_body_dict])

    # 更新引用计数记录的 count 值
    map(bulk_dec_ref_counter,
        [request_headers_dict, request_body_dict,
         response_headers_dict, response_body_dict],
        [RefRequestHeaders, RefRequestBody,
         RefResponseHeaders, RefResponseBody]
        )

    # 删除 header 和 body 的 grid_fs 文件
    id_list = RefRequestHeaders.objects(count__lte=0).only('id')
    AccessLogRequest.delete_headers_files([t.id for t in id_list])

    id_list = RefRequestBody.objects(count__lte=0).only('id')
    AccessLogRequest.delete_body_files([t.id for t in id_list])

    id_list = RefResponseHeaders.objects(count__lte=0).only('id')
    AccessLogResponse.delete_headers_files([t.id for t in id_list])

    id_list = RefResponseBody.objects(count__lte=0).only('id')
    AccessLogResponse.delete_body_files([t.id for t in id_list])

    # 删除 count 为 0 的 引用计数记录
    RefRequestHeaders.objects(count=0).delete()
    RefRequestBody.objects(count=0).delete()
    RefResponseHeaders.objects(count=0).delete()
    RefResponseBody.objects(count=0).delete()

    # 删除 access_log
    AccessLog.objects(id__in=[t.id for t in logs]).delete()
    return len(logs)


@app.task
def clear_old_access_logs():
    """
    清理过期的访问日志数据
    :return:
    """
    logger.info('执行 clear_old_access_logs')
    access_log_keep_days = settings.ACCESS_LOG_KEEP_DAYS
    expires = datetime.utcnow() - timedelta(days=access_log_keep_days)
    logger.debug(expires)
    limit = 1000
    while True:
        count = do_clear_old_access_logs(expires, limit)
        logger.info('清理%s条日志' % count)
        if count < limit:
            break

    logger.info('执行 clear_old_access_logs 完成')


def bulk_dec_ref_counter(data_list, model):
    bulk_operations = []
    for k, v in iteritems(data_list):
        try:
            # 只有 $inc, 没有 $dec
            update_dict = {'$inc': {'count': -1 * v}}
            filter_dict = {'_id': k}
            # logger.debug(filter_dict)
            bulk_operations.append(
                UpdateOne(filter_dict, update_dict))

        except ValidationError as e:
            logger.error(e)

    if bulk_operations:
        collection = model._get_collection() \
            .bulk_write(bulk_operations, ordered=False)


def bulk_update_counter(entities, model):
    bulk_operations = []
    for entity in entities:
        try:
            update_fields = entity.to_mongo().to_dict()
            update_dict = {}
            if 'count' in update_fields:
                count = update_fields['count']
                del update_fields['count']
                update_dict['$inc'] = {'count': count}

            logger.debug(update_fields)
            update_dict['$set'] = update_fields
            entity.validate()

            filter_dict = update_fields
            logger.debug(filter_dict)
            bulk_operations.append(
                UpdateOne(filter_dict,
                          update_dict,
                          upsert=True))

        except ValidationError as e:
            logger.error(e)

    if bulk_operations:
        collection = model._get_collection() \
            .bulk_write(bulk_operations, ordered=False)


def do_parse_access_logs(limit):
    logger.debug('do_parse_access_logs')
    logs = AccessLog.objects(flag__ne=True).no_dereference().only(
        'client.id', 'endpoint.id', 'accessed_at',
        'result_code').order_by('-id')[:limit]
    logger.debug(len(logs))
    access_day_counter_dict = {}
    access_hour_counter_dict = {}
    result_day_counter_dict = {}
    result_hour_counter_dict = {}
    total_day_counter_dict = {}
    total_hour_counter_dict = {}

    for t in logs:
        t.date = t.accessed_at.date()
        t.date_hour = datetime(t.date.year, t.date.month,
                               t.date.day, t.accessed_at.hour)
        logger.debug(t.date_hour)
        # 按日期计算访问量
        key = t.date.strftime('%Y%m%d')
        counter = total_day_counter_dict.get(key)
        if counter:
            counter.count += 1
        else:
            counter = AccessTotalDayCounter()
            # counter.id = key
            counter.date = t.date
            counter.count = 1
            total_day_counter_dict[key] = counter

        # 按小时计算访问量
        key = '%s-%s-%s' % (t.date_hour.strftime('%Y%m%d%H'),
                            id_to_str(t.client.id),
                            id_to_str(t.endpoint.id))

        counter = total_hour_counter_dict.get(key)
        if counter:
            counter.count += 1
        else:
            counter = AccessTotalHourCounter()
            # counter.id = key
            counter.date = t.date_hour
            counter.count = 1
            total_hour_counter_dict[key] = counter

        # 按日期计算 client endpoint 访问量
        key = '%s-%s-%s' % (t.date.strftime('%Y%m%d'),
                            id_to_str(t.client.id),
                            id_to_str(t.endpoint.id))

        counter = access_day_counter_dict.get(key)
        if counter:
            counter.count += 1
        else:
            counter = AccessDayCounter()
            # counter.id = key
            counter.client_id = t.client.id
            counter.endpoint_id = t.endpoint.id
            counter.date = t.date
            counter.count = 1
            access_day_counter_dict[key] = counter

        # 按小时计算 client endpoint 访问量
        key = '%s-%s-%s' % (t.date_hour.strftime('%Y%m%d%H'),
                            id_to_str(t.client.id),
                            id_to_str(t.endpoint.id))

        counter = access_hour_counter_dict.get(key)
        if counter:
            counter.count += 1
        else:
            counter = AccessHourCounter()
            # counter.id = key
            counter.client_id = t.client.id
            counter.endpoint_id = t.endpoint.id
            counter.date = t.date_hour
            counter.count = 1
            access_hour_counter_dict[key] = counter

        # 按日期计算 result_code 数量
        key = '%s-%s' % (t.date.strftime('%Y%m%d'), t.result_code)

        counter = result_day_counter_dict.get(key)
        if counter:
            counter.count += 1
        else:
            counter = AccessResultDayCounter()
            # counter.id = key
            counter.code = t.result_code
            counter.date = t.date
            counter.count = 1
            result_day_counter_dict[key] = counter

        # 按小时计算 result_code 数量
        key = '%s-%s' % (t.date_hour.strftime('%Y%m%d%H'), t.result_code)

        counter = result_hour_counter_dict.get(key)
        if counter:
            counter.count += 1
        else:
            counter = AccessResultHourCounter()
            # counter.id = key
            counter.code = t.result_code
            counter.date = t.date_hour
            counter.count = 1
            result_hour_counter_dict[key] = counter

    bulk_update_counter(total_day_counter_dict.values(), AccessTotalDayCounter)
    bulk_update_counter(total_hour_counter_dict.values(), AccessTotalHourCounter)
    bulk_update_counter(access_day_counter_dict.values(), AccessDayCounter)
    bulk_update_counter(access_hour_counter_dict.values(), AccessHourCounter)
    bulk_update_counter(result_day_counter_dict.values(), AccessResultDayCounter)
    bulk_update_counter(result_hour_counter_dict.values(), AccessResultHourCounter)

    AccessLog.objects(id__in=[t.id for t in logs]).update(flag=True)

    return len(logs)


def do_transfer_access_logs(r):
    log_item_str = r.lpop(settings.ANALYTICS_LOG_REDIS_LIST_KEY)
    if log_item_str is None:
        return False

    log_item = json.loads(log_item_str)
    access_log = AccessLog()
    access_log.ip = log_item['ip']
    access_log.client = AccessLogClient(**log_item['client'])
    access_log.endpoint = AccessLogEndpoint(**log_item['endpoint'])
    access_log.forward_url = log_item['forward_url']
    access_log.elapsed = log_item['elapsed']
    access_log.result_code = log_item['result_code']
    access_log.result_msg = log_item['result_msg']
    access_log.accessed_at = datetime.fromtimestamp(log_item['accessed_at'] / 1000.0)
    access_log.request = AccessLogRequest()
    access_log.request.content_type = log_item['request']['content_type']
    access_log.request.method = log_item['request']['method']
    access_log.request.uri = log_item['request']['uri']
    access_log.request.save_files(log_item['request']['headers'],
                                  log_item['request']['body'])

    access_log.response = AccessLogResponse()
    access_log.response.content_type = log_item['response']['content_type']
    access_log.response.status = log_item['response']['status']
    access_log.response.save_files(log_item['response']['headers'],
                                   log_item['response']['body'])

    access_log.save()

    return True


@app.task
def transfer_access_logs():
    logger.info('执行 transfer_access_logs')
    r = RedisHelper.get_client()
    count = 0
    should_continue = True
    while should_continue:
        count += 1
        try:
            should_continue = do_transfer_access_logs(r)
        except Exception as e:
            logger.error(e)
            logger.error(traceback.format_exc())

    logger.info('执行 transfer_access_logs 完成, 共计%s项' % count)


@app.task
def parse_access_logs():
    """
    解析访问日志数据
    :return:
    """
    success = True
    logger.info('执行 parse_access_logs')
    try:
        per_time_num = 500
        while True:
            processed_num = do_parse_access_logs(per_time_num)
            if processed_num < per_time_num:
                break

    except Exception as e:
        logger.error('执行 parse_access_logs 出现异常')
        logger.error(e.message)
        logger.error(traceback.format_exc())
        success = False

    logger.info('执行 parse_access_logs 完成')

    return success
