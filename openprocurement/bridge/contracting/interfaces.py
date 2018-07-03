from zope.interface import implementer
from zope.component.interfaces import IFactory
from iso8601 import parse_date
from datetime import datetime, timedelta
from uuid import uuid4

from openprocurement.bridge.contracting.constants import ACCELERATOR_RE, DAYS_PER_YEAR
from openprocurement.bridge.contracting.journal_msg_ids import (
    DATABRIDGE_EXCEPTION,
    DATABRIDGE_COPY_CONTRACT_ITEMS,
    DATABRIDGE_MISSING_CONTRACT_ITEMS
)
from openprocurement.bridge.contracting.utils import (
    fill_base_contract_data,
    handle_common_tenders,
    handle_esco_tenders,
    journal_context,
    logger,
    accelerate_milestones,
    TZ,
    to_decimal
)
from esculator.calculations import discount_rate_days, payments_days, calculate_payments


class IContractCreator(IFactory):
    """markup interface for creating contracts"""


@implementer(IContractCreator)
class BaseCreator(object):

    def __init__(self, contract, tender):
        self.contract = contract
        self.tender = tender

    def create_contract(self):
        self.fill_base_contract_data()

        logger.info('Handle common tender {}'.format(self.tender['id']), extra={"MESSAGE_ID": "handle_common_tenders"})

    def fill_base_contract_data(self):
        self.contract['tender_id'] = self.tender['id']
        self.contract['procuringEntity'] = self.tender['procuringEntity']
        self.contract['contractType'] = 'common'

        # set contract mode
        if self.tender.get('mode'):
            self.contract['mode'] = self.tender['mode']

        # copy items from tender
        if not self.contract.get('items'):
            logger.info(
                'Copying contract {} items'.format(self.contract['id']),
                extra=journal_context(
                    {"MESSAGE_ID": DATABRIDGE_COPY_CONTRACT_ITEMS},
                    {"CONTRACT_ID": self.contract['id'], "TENDER_ID": self.tender['id']}
                )
            )
            if self.tender.get('lots'):
                related_awards = [aw for aw in self.tender['awards'] if aw['id'] == self.contract['awardID']]
                if related_awards:
                    award = related_awards[0]
                    if award.get("items"):
                        logger.debug('Copying items from related award {}'.format(award['id']))
                        self.contract['items'] = award['items']
                    else:
                        logger.debug('Copying items matching related lot {}'.format(award['lotID']))
                        self.contract['items'] = [item for item in self.tender['items'] if
                                             item.get('relatedLot') == award['lotID']]
                else:
                    logger.warn(
                        'Not found related award for contact {} of tender {}'.format(self.contract['id'], self.tender['id']),
                        extra=journal_context(
                            {"MESSAGE_ID": DATABRIDGE_EXCEPTION},
                            params={"CONTRACT_ID": self.contract['id'], "TENDER_ID": self.tender['id']}
                        )
                    )
            else:
                logger.debug(
                    'Copying all tender {} items into contract {}'.format(self.tender['id'], self.contract['id']),
                    extra=journal_context(
                        {"MESSAGE_ID": DATABRIDGE_COPY_CONTRACT_ITEMS},
                        params={"CONTRACT_ID": self.contract['id'], "TENDER_ID": self.tender['id']}
                    )
                )
                self.contract['items'] = self.tender['items']

        # delete `items` key if contract.items is empty list
        if isinstance(self.contract.get('items', None), list) and len(self.contract.get('items')) == 0:
            logger.info(
                "Clearing 'items' key for contract with empty 'items' list",
                extra=journal_context(
                    {"MESSAGE_ID": DATABRIDGE_COPY_CONTRACT_ITEMS},
                    {"CONTRACT_ID": self.contract['id'], "TENDER_ID": self.tender['id']}
                )
            )
            del self.contract['items']

        if not self.contract.get('items'):
            logger.warn(
                'Contract {} of tender {} does not contain items info'.format(self.contract['id'], self.tender['id']),
                extra=journal_context(
                    {"MESSAGE_ID": DATABRIDGE_MISSING_CONTRACT_ITEMS},
                    {"CONTRACT_ID": self.contract['id'], "TENDER_ID": self.tender['id']}
                )
            )

        for item in self.contract.get('items', []):
            if 'deliveryDate' in item and item['deliveryDate'].get('startDate') and item['deliveryDate'].get(
                    'endDate'):
                if item['deliveryDate']['startDate'] > item['deliveryDate']['endDate']:
                    logger.info(
                        "Found dates missmatch {} and {}".format(
                            item['deliveryDate']['startDate'], item['deliveryDate']['endDate']
                        ),
                        extra=journal_context(
                            {"MESSAGE_ID": DATABRIDGE_EXCEPTION},
                            params={"CONTRACT_ID": self.contract['id'], "TENDER_ID": self.tender['id']}
                        )
                    )
                    del item['deliveryDate']['startDate']
                    logger.info(
                        "startDate value cleaned.",
                        extra=journal_context(
                            {"MESSAGE_ID": DATABRIDGE_EXCEPTION},
                            params={"CONTRACT_ID": self.contract['id'], "TENDER_ID": self.tender['id']}
                        )
                    )


class EscoCreator(BaseCreator):

    def __init__(self, contract, tender):
        super(EscoCreator, self).__init__(contract, tender)

    def create_contract(self):
        super(EscoCreator, self).create_contract()

        self.contract['contractType'] = 'esco'
        if 'procurementMethodDetails' in self.tender:
            self.contract['procurementMethodDetails'] = self.tender['procurementMethodDetails']
        logger.info('Handle esco tender {}'.format(self.tender['id']), extra={"MESSAGE_ID": "handle_esco_tenders"})

        keys = ['NBUdiscountRate', 'noticePublicationDate']
        keys_from_lot = ['fundingKind', 'yearlyPaymentsPercentageRange', 'minValue']

        # fill contract values from lot
        if self.tender.get('lots'):
            related_awards = [aw for aw in self.tender['awards'] if aw['id'] == self.contract['awardID']]
            if related_awards:
                lot_id = related_awards[0]['lotID']
                related_lots = [lot for lot in self.tender['lots'] if lot['id'] == lot_id]
                if related_lots:
                    logger.debug('Fill contract {} values from lot {}'.format(self.contract['id'], related_lots[0]['id']))
                    for key in keys_from_lot:
                        self.contract[key] = related_lots[0][key]
                else:
                    logger.critical(
                        'Not found related lot for contract {} of tender {}'.format(self.contract['id'], self.tender['id']),
                        extra={'MESSAGE_ID': 'not_found_related_lot'}
                    )
                    keys += keys_from_lot
            else:
                logger.warn(
                    'Not found related award for contract {} of tender {}'.format(self.contract['id'], self.tender['id']))
                keys += keys_from_lot
        else:
            keys += keys_from_lot

        for key in keys:
            self.contract[key] = self.tender[key]
        self.contract['milestones'] = self.generate_milestones()

    def generate_milestones(self):
        accelerator = 0
        if 'procurementMethodDetails' in self.contract:
            re_obj = ACCELERATOR_RE.search(self.contract['procurementMethodDetails'])
            if re_obj and 'accelerator' in re_obj.groupdict():
                accelerator = int(re_obj.groupdict()['accelerator'])

        npv_calculation_duration = 20
        announcement_date = parse_date(self.tender['noticePublicationDate'])

        contract_days = timedelta(days=self.contract['value']['contractDuration']['days'])
        contract_years = timedelta(days=self.contract['value']['contractDuration']['years'] * DAYS_PER_YEAR)
        date_signed = parse_date(self.contract['dateSigned'])
        signed_delta = date_signed - announcement_date
        if 'period' not in self.contract or ('mode' in self.contract and self.contract['mode'] == 'test'):
            contract_end_date = announcement_date + contract_years + contract_days
            if accelerator:
                real_date_signed = announcement_date + timedelta(seconds=signed_delta.total_seconds() * accelerator)
                self.contract['dateSigned'] = real_date_signed.isoformat()

            self.contract['period'] = {
                'startDate': self.contract['dateSigned'],
                'endDate': contract_end_date.isoformat()
            }

        # set contract.period.startDate to contract.dateSigned if missed
        if 'startDate' not in self.contract['period']:
            self.contract['period']['startDate'] = self.contract['dateSigned']

        contract_start_date = parse_date(self.contract['period']['startDate'])
        contract_end_date = parse_date(self.contract['period']['endDate'])

        contract_duration_years = self.contract['value']['contractDuration']['years']
        contract_duration_days = self.contract['value']['contractDuration']['days']
        yearly_payments_percentage = self.contract['value']['yearlyPaymentsPercentage']
        annual_cost_reduction = self.contract['value']['annualCostsReduction']

        days_for_discount_rate = discount_rate_days(announcement_date, DAYS_PER_YEAR, npv_calculation_duration)
        days_with_payments = payments_days(
            contract_duration_years, contract_duration_days, days_for_discount_rate, DAYS_PER_YEAR,
            npv_calculation_duration
        )

        payments = calculate_payments(
            yearly_payments_percentage, annual_cost_reduction, days_with_payments, days_for_discount_rate
        )

        milestones = []

        logger.info("Generate milestones for esco tender {}".format(self.tender['id']))
        max_contract_end_date = contract_start_date + timedelta(days=DAYS_PER_YEAR * 15)

        sequence_number = 1
        while True:
            date_modified = datetime.now(TZ)
            milestone = {
                'id': uuid4().hex,
                'sequenceNumber': sequence_number,
                'date': date_modified.isoformat(),
                'dateModified': date_modified.isoformat(),
                'amountPaid': {
                    "amount": 0,
                    "currency": self.contract['value']['currency'],
                    "valueAddedTaxIncluded": self.contract['value']['valueAddedTaxIncluded']
                },
                'value': {
                    "amount": to_decimal(payments[sequence_number - 1]) if sequence_number <= 21 else 0.00,
                    "currency": self.contract['value']['currency'],
                    "valueAddedTaxIncluded": self.contract['value']['valueAddedTaxIncluded']
                },
            }
            if sequence_number == 1:
                milestone_start_date = announcement_date
                milestone_end_date = TZ.localize(datetime(announcement_date.year + sequence_number, 1, 1))
                milestone['status'] = 'pending'
            else:
                milestone_start_date = TZ.localize(datetime(announcement_date.year + sequence_number - 1, 1, 1))
                milestone_end_date = TZ.localize(datetime(announcement_date.year + sequence_number, 1, 1))

            if contract_end_date.year == milestone_start_date.year:
                milestone_end_date = contract_end_date

            if milestone_start_date > max_contract_end_date:
                break

            milestone['period'] = {
                'startDate': milestone_start_date.isoformat(),
                'endDate': milestone_end_date.isoformat()
            }

            if contract_end_date.year >= milestone_start_date.year and sequence_number != 1:
                milestone['status'] = 'scheduled'
            elif contract_end_date.year < milestone_start_date.year:
                milestone['status'] = 'spare'

            title = "Milestone #{} of year {}".format(sequence_number, milestone_start_date.year)
            milestone['title'] = title
            milestone['description'] = title

            milestones.append(milestone)
            sequence_number += 1
        milestones[-1]['period']['endDate'] = max_contract_end_date.isoformat()

        if accelerator:
            accelerate_milestones(milestones, DAYS_PER_YEAR, accelerator)
            # restore accelerated contract.dateSigned
            self.contract['dateSigned'] = date_signed.isoformat()
            # accelerate contract.period.endDate
            delta = contract_days + contract_years
            contract_end_date = announcement_date + timedelta(seconds=delta.total_seconds() / accelerator)
            self.contract['period'] = {
                'startDate': self.contract['dateSigned'],
                'endDate': contract_end_date.isoformat()
            }
        return milestones

