from dateutil import easter
import datetime
import bisect
import math
import json
import os
import sys
import pandas as pd
from requests import post
import logging
import traceback
from sqlalchemy import Table, select, and_


def make_data_path():
    if os.path.lexists("../data"):
        return
    else:
        os.symlink("/config/dao_data", "../data")


def is_laagtarief(dtime, switch_hour):
    jaar = dtime.year
    datum = datetime.datetime(dtime.year, dtime.month, dtime.day)
    if datum.weekday() >= 5:  # zaterdag en zondag
        return True
    if (dtime.hour < 7) or (dtime.hour >= switch_hour):  # door de week van 7 tot 21/23
        return True
    feestdagen = [datetime.datetime(jaar, 1, 1), datetime.datetime(jaar, 4, 27), datetime.datetime(jaar, 12, 25),
                  datetime.datetime(jaar, 12, 26)]
    pasen = easter.easter(jaar)
    feestdagen.append(pasen + datetime.timedelta(days=1))  # 2e paasdag
    feestdagen.append(pasen + datetime.timedelta(days=39))  # hemelvaart
    feestdagen.append(pasen + datetime.timedelta(days=50))  # 2e pinksterdag

    for day in feestdagen:
        if day == datum:  # dag is een feestdag
            return True
    return False


def calc_adjustment_heatcurve(price_act: float, price_avg: float, adjustment_factor, old_adjustment: float) -> float:
    """
    Calculate the adjustment of the heatcurve
    formule: -0,5*(price-price_avg)*10/price_avg
    :param price_act: the actual hourprice
    :param price_avg: the day average of the price
    :param adjustment_factor: factor in K/% for instance 0,4K per 10% = 0.04 K/%
    :param old_adjustment: current/old adjustment
    :return: the calculated adjustment
    """
    if price_avg == 0:
        adjustment = 0
    else:
        adjustment = round(- adjustment_factor * (price_act - price_avg) * 100 / price_avg, 1)
    # toename en afname maximeren op 10 x adjustment factor
    if adjustment >= old_adjustment:
        adjustment = min(adjustment, old_adjustment + adjustment_factor * 10)
    else:
        adjustment = max(adjustment, old_adjustment - adjustment_factor * 10)
    return round(adjustment, 1)


def get_value_from_dict(dag: str, options: dict) -> float:
    """
    Selecteert uit een dict van datum/value paren de juiste value
    :param dag: string van de dag format yyyy-mm-dd
    :param options: dict van datum/value paren bijv. {'2022-01-01': 0.002, '2023-03-01': 0.018}
    :return: de correcte value
    """
    o_list = list(options.keys())
    result = options.get(
        dag, options[o_list[bisect.bisect_left(o_list, dag) - 1]])
    return result


def convert_timestr(time_str: str, now_dt: datetime.datetime) -> datetime.datetime:
    result_hm = datetime.datetime.strptime(time_str, '%H:%M:%S')
    result = datetime.datetime(now_dt.year, now_dt.month, now_dt.day, result_hm.hour, result_hm.minute)
    return result


def get_tibber_data():
    from da_config import Config
    from db_manager import DBmanagerObj

    def get_datetime_from_str(s):
        # "2022-09-01T01:00:00.000+02:00"
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")

    # Generate the list of timestamps
    def generate_hourly_timestamps(start_gen: float, end_gen: float) -> list:
        all_hours = []
        current_ts = start_gen
        while current_ts <= end_gen:
            all_hours.append(current_ts)
            current_ts += 3600
        return all_hours

    config = Config("../data/options.json")
    tibber_options = config.get(["tibber"])
    url = config.get(["api url"], tibber_options, "https://api.tibber.com/v1-beta/gql")
    db_da_engine = config.get(['database da', "engine"], None, "mysql")
    db_da_server = config.get(['database da', "server"], None, "core-mariadb")
    db_da_port = int(config.get(['database da', "port"], None, 3306))
    db_da_name = config.get(['database da', "database"], None, "day_ahead")
    db_da_user = config.get(['database da', "username"], None, "day_ahead")
    db_da_password = config.get(['database da', "password"])
    db_time_zone = config.get(["time_zone"])
    db_da = DBmanagerObj(db_dialect=db_da_engine, db_name=db_da_name, db_server=db_da_server, db_port=db_da_port,
                         db_user=db_da_user, db_password=db_da_password, db_time_zone=db_time_zone)
    prices_options = config.get(["prices"])
    headers = {
        "Authorization": "Bearer " + tibber_options["api_token"],
        "content-type": "application/json",
    }
    now_ts = latest_ts = math.ceil(datetime.datetime.now().timestamp() / 3600) * 3600
    start_ts = None
    if len(sys.argv) > 2:
        # datetime start is given
        start_str = sys.argv[2]
        try:
            start_ts = datetime.datetime.strptime(start_str, "%Y-%m-%d").timestamp()
            timestamps = generate_hourly_timestamps(start_ts, now_ts)
            latest_ts = start_ts
        except Exception as ex:
            error_handling(ex)
            return

    # no starttime
    if (len(sys.argv) <= 2) or (start_ts is None):
        # search first missing
        start_ts = datetime.datetime.strptime(prices_options["last invoice"], "%Y-%m-%d").timestamp()
        timestamps = generate_hourly_timestamps(start_ts, now_ts)
        values_table = Table('values', db_da.metadata, autoload_with=db_da.engine)
        variabel_table = Table('variabel', db_da.metadata, autoload_with=db_da.engine)
        for code in ['cons', 'prod']:
            # Query the existing timestamps from the values table
            query = select(values_table.c.time).where(
                and_(
                    variabel_table.c.code == code,
                    variabel_table.c.id == values_table.c.variabel,
                    values_table.c.time.between(start_ts, now_ts),
                )
            )
            with db_da.engine.connect() as connection:
                existing_timestamps = {row[0] for row in connection.execute(query)}

            # Find missing timestamps by comparing the generated list with the existing timestamps
            missing_timestamps = [ts for ts in timestamps if ts not in existing_timestamps]
            if len(missing_timestamps) == 0:
                latest = start_ts
            else:
                latest = missing_timestamps[0]
            latest_ts = min(latest_ts, latest)

    count = math.ceil((now_ts - latest_ts)/3600)
    logging.info(f"Tibber data present tot en met: {str(datetime.datetime.fromtimestamp(latest_ts - 3600))}")
    if count < 24:
        logging.info("Er worden geen data opgehaald.")
        return
    query = '{ ' \
            '"query": ' \
            ' "{ ' \
            '   viewer { ' \
            '     homes { ' \
            '      production(resolution: HOURLY, last: '+str(count)+') { ' \
            '        nodes { ' \
            '          from ' \
            '          profit ' \
            '          production ' \
            '        } ' \
            '      } ' \
            '    consumption(resolution: HOURLY, last: '+str(count)+') { ' \
            '        nodes { ' \
            '          from ' \
            '          cost ' \
            '          consumption ' \
            '        } ' \
            '      } ' \
            '    } ' \
            '  } ' \
            '}" ' \
        '}'

    logging.debug(query)
    resp = post(url, headers=headers, data=query)
    tibber_dict = json.loads(resp.text)
    production_nodes = tibber_dict['data']['viewer']['homes'][0]['production']['nodes']
    consumption_nodes = tibber_dict['data']['viewer']['homes'][0]['consumption']['nodes']
    tibber_df = pd.DataFrame(columns=['time', 'code', 'value'])
    for node in production_nodes:
        time_stamp = str(int(get_datetime_from_str(node['from']).timestamp()))
        if not (node["production"] is None):
            code = "prod"
            value = float(node["production"])
            logging.info(f"{node} {time_stamp} {value}")
            tibber_df.loc[tibber_df.shape[0]] = [time_stamp, code, value]
        if not (node["profit"] is None):
            code = 'profit'
            value = float(node["profit"])
            logging.info(f"{node} {time_stamp} {value}")
            tibber_df.loc[tibber_df.shape[0]] = [time_stamp, code, value]
    for node in consumption_nodes:
        time_stamp = str(int(get_datetime_from_str(node['from']).timestamp()))
        if not (node["consumption"] is None):
            code = "cons"
            value = float(node["consumption"])
            logging.info(f"{node} {time_stamp} {value}")
            tibber_df.loc[tibber_df.shape[0]] = [time_stamp, code, value]
        if not (node["cost"] is None):
            code = "cost"
            value = float(node["cost"])
            logging.info(f"{node} {time_stamp} {value}")
            tibber_df.loc[tibber_df.shape[0]] = [time_stamp, code, value]
    logging.info(f"Opgehaalde data bij Tibber (database records):"
                 f"\n{tibber_df.to_string(index=False)}")
    db_da.savedata(tibber_df)


def calc_uur_index(dt: datetime, tijd: list) -> int:
    """
    Berekent van parameter dt de index in lijst uur
    :param dt: de datetime waarvan de index wordt gezocht
    :param tijd: lijst met datetime van begin van het betreffende uur
    :return: het indexnummer in de lijst
    """
    result_index = len(tijd)
    if (result_index == 0) or (dt < tijd[0]):
        return result_index
    for u in range(len(tijd)):
        if dt < (tijd[u] + datetime.timedelta(hours=1)):
            result_index = u
            break
    return result_index


'''
def calc_heatpump_usage
    (pl : [], needed : float) ->[]:
    """
    berekent inzet van de wp per uur
    :param pl: een list van de inkoop prijzen
    :param needed:  benodige Wh aan energie
    :return: een list van Wh in de betreffende uren
    """
    U = len(pl) # aantal uur
    pl_min = min(pl)
    sum_cost = 0
    max_low = U * 250
    usage = []
    if max_low >= needed:
        #alleen de goedkopere uren inzetten
    else:
        #alle uren minimum inzetten plus nog wat extra
        for u in range(U):
            sum_cost += pl[u]-pl_min
        extra_energy = needed - max_low
        energy_cost = sum_cost/extra_energy
        for u in range(U):
            usage.append(250+ (pl[u]-pl_min) * energy_cost)
'''


def get_version():
    return __version__


def version_number(version_str: str) -> int:
    lst = [int(x, 10) for x in version_str.split('.')]
    lst.reverse()
    return sum(x * (100 ** i) for i, x in enumerate(lst))


def log_exc_plus():
    """
    Print the usual traceback information,
    """
    tb = sys.exc_info()[2]
    while 1:
        if not tb.tb_next:
            break
        tb = tb.tb_next
    stack = []
    f = tb.tb_frame
    while f:
        stack.append(f)
        f = f.f_back
    stack.reverse()
    traceback.print_exc()
    for frame in stack:
        logging.error(f"File: {frame.f_code.co_filename}, line {frame.f_lineno}, in {frame.f_code.co_name}")


def error_handling(ex):
    if logging.root.level == logging.DEBUG:
        logging.exception(ex)
    else:
        log_exc_plus()
