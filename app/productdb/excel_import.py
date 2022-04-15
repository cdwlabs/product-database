import datetime
import logging
from zipfile import BadZipFile

import pandas as pd
from django.core.exceptions import ValidationError
from django.db import transaction
from app.productdb.models import Product, CURRENCY_CHOICES, ProductGroup, ProductMigrationSource, ProductMigrationOption
from app.productdb.models import Vendor

logger = logging.getLogger("productdb")


class InvalidExcelFileFormat(Exception):
    """Exception thrown if there is an issue with the low level file format"""
    pass


class InvalidImportFormatException(Exception):
    """Exception thrown if the format of the Excel file for the import is invalid"""
    pass


class BaseExcelImporter:
    """
    Base class for the Excel Import
    """
    sheetname = "products"
    required_keys = {"product id", "description", "list price", "vendor"}
    import_converter = None
    drop_na_columns = None
    workbook = None
    path = None
    valid_file = False
    user_for_revision = None
    __wb_data_frame__ = None
    import_result_messages = None

    def __init__(self, path_to_excel_file=None, user_for_revision=None):
        self.path_to_excel_file = path_to_excel_file
        if self.import_result_messages is None:
            self.import_result_messages = []
        if self.import_converter is None:
            self.import_converter = {}
        if self.drop_na_columns is None:
            self.drop_na_columns = []
        if user_for_revision:
            self.user_for_revision = user_for_revision

    def _load_workbook(self):
        try:
            self.workbook = pd.ExcelFile(self.path_to_excel_file, engine="openpyxl")

        except BadZipFile as ex:
            logger.error("invalid format of excel file '%s' (%s)" % (self.path_to_excel_file, ex), exc_info=True)
            raise InvalidExcelFileFormat("invalid file format") from ex

        except Exception:
            logger.fatal("unable to read workbook at '%s'" % self.path_to_excel_file, exc_info=True)
            raise

    def _create_data_frame(self):
        self.__wb_data_frame__ = self.workbook.parse(
            self.sheetname, converters=self.import_converter
        )

        # normalize the column names (all lowercase, strip whitespace if any)
        self.__wb_data_frame__.columns = [x.lower() for x in self.__wb_data_frame__.columns]
        self.__wb_data_frame__.columns = [x.strip() for x in self.__wb_data_frame__.columns]

        # drop NA columns if defined
        if len(self.drop_na_columns) != 0:
            self.__wb_data_frame__.dropna(axis=0, subset=self.drop_na_columns, inplace=True)

    def verify_file(self):
        if self.workbook is None:
            self._load_workbook()

        self.valid_file = False

        sheets = self.workbook.sheet_names

        # verify worksheet that is required
        if self.sheetname not in sheets:
            raise InvalidImportFormatException("sheet '%s' not found" % self.sheetname)

        # verify keys in file
        dframe = self.workbook.parse(self.sheetname)
        keys = [x.lower() for x in set(dframe.keys())]

        if len(self.required_keys.intersection(keys)) != len(self.required_keys):
            req_key_str = ", ".join(sorted(self.required_keys))
            raise InvalidImportFormatException("not all required keys are found in the Excel file, required keys "
                                               "are: %s" % req_key_str)

        self.valid_file = True

    def is_valid_file(self):
        return self.valid_file

    @staticmethod
    def _import_datetime_column_from_file(row_key, row, target_key, product):
        """
        helper method to import an optional columns from the excel file
        """
        changed = False
        faulty_entry = False
        msg = ""
        try:
            if row_key in row:
                currval = getattr(product, target_key)
                if not pd.isnull(row[row_key]):
                    if (type(row[row_key]) is pd.Timestamp) or (type(row[row_key]) is datetime.datetime):
                        newval = row[row_key].date()

                    else:
                        newval = None

                    if currval != newval:
                        setattr(product, target_key, row[row_key].date())
                        changed = True

                else:
                    # if row is present, but no date value is set, update the value to null
                    if currval is not None:
                        setattr(product, target_key, None)
                        changed = True

        except Exception as ex:  # catch any exception
            faulty_entry = True
            msg = "cannot set %s for <code>%s</code> (%s)" % (row_key, row["product id"], ex)

        return changed, faulty_entry, msg

    def import_to_database(self, status_callback=None, update_only=False):
        """
        Base method that is triggered for the update
        """
        pass


class ProductsExcelImporter(BaseExcelImporter):
    """
    Excel Importer class for Products
    """
    sheetname = "products"
    required_keys = {"product id", "description", "list price", "vendor"}
    import_converter = {
        "product id": str,
        "description": str,
        "list price": str,
        "currency": str,
        "vendor": str,
        "tags": str
    }
    drop_na_columns = ["product id"]
    valid_imported_products = 0
    invalid_products = 0

    @property
    def amount_of_products(self):
        return len(self.__wb_data_frame__) if self.__wb_data_frame__ is not None else -1

    def import_to_database(self, status_callback=None, update_only=False):
        """
        Import products from the associated excel sheet to the database
        :param status_callback: optional status message callback function
        :param update_only: don't create new entries
        """
        if self.workbook is None:
            self._load_workbook()
        if self.__wb_data_frame__ is None:
            self._create_data_frame()

        self.valid_imported_products = 0
        self.invalid_products = 0
        self.import_result_messages.clear()
        amount_of_entries = len(self.__wb_data_frame__.index)

        # process entries in file
        current_entry = 1
        for index, row in self.__wb_data_frame__.iterrows():
            # update status message if defined
            if status_callback and (current_entry % 100 == 0):
                status_callback("Process entry <strong>%s</strong> of "
                                "<strong>%s</strong>..." % (current_entry, amount_of_entries))

            faulty_entry = False        # indicates an invalid entry
            created = False             # indicates that the product was created
            skip = False                # skip the current entry (used in update_only mode)
            msg = "import successful"   # message to describe the result of the product import

            # get vendor name and look for object
            vendor_name = row["vendor"]
            v = Vendor.objects.get(id=0)
            if not pd.isnull(row["vendor"]):
                qs_v = Vendor.objects.filter(name=str(vendor_name).strip())
                if qs_v.count() == 0:
                    raise Exception("unknown vendor '%s'" % row["vendor"])

                else:
                    v = qs_v.first()

            if update_only:
                try:
                    p = Product.objects.get(product_id=row["product id"], vendor=v)

                except Product.DoesNotExist:
                    # element doesn't exist
                    skip = True

                except Exception as ex:  # catch any exception
                    logger.warning("unexpected exception occurred during the lookup "
                                   "of product %s (%s)" % (row["product id"], ex))
                    # skip faulty entry
                    skip = True

            else:
                p, created = Product.objects.get_or_create(product_id=row["product id"], vendor=v)

            changed = created

            if not skip:
                # apply changes (only if a value is set, otherwise ignore it)
                try:
                    row_key = "description"
                    # set the description value
                    if not pd.isnull(row[row_key]):
                        if p.description != row[row_key]:
                            p.description = row[row_key]
                            changed = True

                    # determine the list price and currency from the excel file
                    row_key = "list price"
                    new_currency = "USD"    # default in model
                    if not pd.isnull(row[row_key]):
                        if type(row[row_key]) == float:
                            new_price = row[row_key]

                        elif type(row[row_key]) == int:
                            new_price = float(row[row_key])

                        elif type(row[row_key]) == str:
                            price = row[row_key].split(" ")
                            if len(price) == 1:
                                # only a number
                                new_price = float(row[row_key])

                            elif len(price) == 2:
                                # contains a number and a currency
                                try:
                                    new_price = float(price[0])

                                except:
                                    raise Exception("cannot convert price information to float")

                                # check valid currency value
                                valid_currency = True if price[1].upper() in dict(CURRENCY_CHOICES).keys() else False
                                if valid_currency:
                                    new_currency = price[1].upper()

                                else:
                                    raise Exception("cannot set currency unknown value %s" % price[1].upper())

                            else:
                                raise Exception("invalid format for list price, detected multiple spaces")

                        else:
                            logger.debug("list price data type for %s identified as %s" % (
                                row["product id"],
                                str(type(row[row_key]))
                            ))
                            raise Exception("invalid data-type for list price")
                    else:
                        new_price = None

                    row_key = "currency"
                    if row_key in row:
                        if not pd.isnull(row[row_key]):
                            # check valid currency value
                            valid_currency = True if row[row_key].upper() in dict(CURRENCY_CHOICES).keys() else False
                            if valid_currency:
                                new_currency = row[row_key].upper()

                            else:
                                raise Exception("cannot set currency unknown value %s" % row[row_key].upper())

                    # apply the new list price and currency if required
                    if new_price is not None:
                        if p.list_price != new_price:
                            p.list_price = new_price
                            changed = True
                        if p.currency != new_currency:
                            p.currency = new_currency
                            changed = True

                    # create product group is not existing and product group (optional)
                    row_key = "product group"
                    if row_key in row:  # optional key
                        if not pd.isnull(row[row_key]):
                            if (not p.product_group) or (p.product_group.name != row[row_key]):
                                pg, _ = ProductGroup.objects.get_or_create(name=row[row_key].strip(), vendor=p.vendor)
                                p.product_group = pg
                                changed = True

                        else:
                            # reset value if column is present but no value is set
                            if p.product_group is not None:
                                p.product_group = None
                                changed = True

                    # set Eol note URL and friendly name (both optional)
                    row_key = "eol note url"
                    if row_key in row:  # optional key
                        if not pd.isnull(row[row_key]):
                            if p.eol_reference_url != row[row_key]:
                                p.eol_reference_url = row[row_key]
                                changed = True

                        else:
                            # reset value if column is present but no value is set
                            if p.eol_reference_url is not None or p.eol_reference_url != "":
                                p.eol_reference_url = None
                                changed = True

                    row_key = "eol note url (friendly name)"
                    if row_key in row:  # optional key
                        if not pd.isnull(row[row_key]):
                            if p.eol_reference_number != row[row_key]:
                                p.eol_reference_number = row[row_key]

                        else:
                            # reset value if column is present but no value is set
                            if p.eol_reference_number is not None or p.eol_reference_number != "":
                                p.eol_reference_number = None
                                changed = True

                    # set internal product ID (optional)
                    row_key = "internal product id"
                    if row_key in row:
                        if not pd.isnull(row[row_key]):
                            if p.internal_product_id != row[row_key]:
                                p.internal_product_id = row[row_key]
                                changed = True

                        else:
                            # reset value if column is present but no value is set
                            if p.internal_product_id is not None or p.internal_product_id != "":
                                p.internal_product_id = ""
                                changed = True

                    # set tags field (optional)
                    row_key = "tags"
                    if row_key in row:  # optional key
                        if not pd.isnull(row[row_key]):
                            if p.tags != row[row_key]:
                                p.tags = row[row_key]
                                changed = True

                        else:
                            # reset value if column is present but no value is set
                            if p.tags is not None or p.tags != "":
                                p.tags = ""
                                changed = True

                except Exception as ex:
                    faulty_entry = True
                    msg = "cannot set %s for <code>%s</code> (%s)" % (row_key, row["product id"], ex)

                # import datetime columns from file (all optional, overwrite if None)
                data_map = {
                    # product attribute - data frame column name (lowered during the import)
                    "eox_update_time_stamp": "eox update timestamp",
                    "eol_ext_announcement_date": "eol announcement date",
                    "end_of_sale_date": "end of sale date",
                    "end_of_new_service_attachment_date": "end of new service attachment date",
                    "end_of_sw_maintenance_date": "end of sw maintenance date",
                    "end_of_routine_failure_analysis": "end of routing failure analysis date",
                    "end_of_service_contract_renewal": "end of service contract renewal date",
                    "end_of_support_date": "last date of support",
                    "end_of_sec_vuln_supp_date": "end of security/vulnerability support date"
                }

                for key in data_map.keys():
                    c, f, ret_msg = self._import_datetime_column_from_file(data_map[key], row, key, p)
                    if c:
                        # value was changed
                        changed = True
                    if f:
                        # value was faulty
                        msg = ret_msg
                        faulty_entry = True
                        break

                # save result to database if any
                try:
                    if changed:
                        # update element
                        p.save()
                        self.valid_imported_products += 1

                        # add import result message
                        if created:
                            self.import_result_messages.append("product <code>%s</code> created" % p.product_id)

                        else:
                            self.import_result_messages.append("product <code>%s</code> updated" % p.product_id)

                    else:
                        self.import_result_messages.append("<i>no changes for product "
                                                           "<code>%s</code> required</i>" % p.product_id)

                except Exception as ex:
                    faulty_entry = True
                    msg = "cannot save data for <code>%s</code> in database (%s)" % (row["product id"], ex)

                if faulty_entry:
                    logger.error("cannot import %s (%s)" % (row["product id"], msg))
                    self.import_result_messages.append(msg)
                    self.invalid_products += 1

                    # terminate the process after 30 errors
                    if self.invalid_products > 30:
                        self.import_result_messages.append("There are too many errors in your file, please "
                                                           "correct them and upload it again")
                        break

            current_entry += 1


class ProductMigrationsExcelImporter(BaseExcelImporter):
    """
    Excel Importer class for Product Migrations
    """
    sheetname = "product_migrations"
    required_keys = {"product id", "vendor", "migration source"}
    drop_na_columns = [
        "product id",
        "migration source"
    ]
    import_converter = {
        "product id": str,
        "vendor": str,
        "migration source": str,
        "replacement product id": str,
        "comment": str,
        "migration product info url": str
    }

    def import_to_database(self, status_callback=None, update_only=False):
        """
        Import products from the associated excel sheet to the database
        :param status_callback: optional status message callback function
        :param update_only: don't create new entries
        """
        if self.workbook is None:
            self._load_workbook()
        if self.__wb_data_frame__ is None:
            self._create_data_frame()

        # process entries in file
        self.import_result_messages = []
        current_entry = 1
        amount_of_entries = len(self.__wb_data_frame__.index)
        for index, row in self.__wb_data_frame__.iterrows():
            # update status message if defined
            if status_callback:
                status_callback("Process entry <strong>%s</strong> of "
                                "<strong>%s</strong>..." % (current_entry, amount_of_entries))

            if row["product id"] == "" or row["product id"] is None:
                continue

            # check that product is part of the database
            try:
                with transaction.atomic():
                    vendor_name = row["vendor"]
                    v = Vendor.objects.get(id=0)
                    if not pd.isnull(row["vendor"]):
                        qs_v = Vendor.objects.filter(name=str(vendor_name).strip())
                        if qs_v.count() == 0:
                            raise Exception("unknown vendor '%s'" % row["vendor"])

                        else:
                            v = qs_v.first()

                    # update element
                    product = Product.objects.get(product_id=row["product id"], vendor=v)
                    migration_source, created = ProductMigrationSource.objects.get_or_create(
                        name=row["migration source"]
                    )

                    if created:
                        migration_source.preference = 10
                        migration_source.save()
                        self.import_result_messages.append("Product Migration Source \"%s\" was created with a "
                                                           "preference of 10" % row["migration source"])

                    pmo, created = ProductMigrationOption.objects.get_or_create(product=product,
                                                                                migration_source=migration_source)
                    row_key = "comment"
                    if row_key in row:  # optional key
                        if not pd.isnull(row[row_key]):
                            if pmo.comment != row[row_key]:
                                pmo.comment = row[row_key]

                    row_key = "replacement product id"
                    if row_key in row:  # optional key
                        if not pd.isnull(row[row_key]):
                            if pmo.replacement_product_id != row[row_key]:
                                pmo.replacement_product_id = row[row_key]

                    row_key = "migration product info url"
                    if row_key in row:  # optional key
                        if not pd.isnull(row[row_key]):
                            if pmo.migration_product_info_url != row[row_key]:
                                pmo.migration_product_info_url = row[row_key]

                    pmo.save()

                    if created:
                        self.import_result_messages.append("create Product Migration path \"%s\" for Product "
                                                           "\"%s\"" % (row["migration source"], row["product id"]))
                    else:
                        self.import_result_messages.append("update Product Migration path \"%s\" for Product "
                                                           "\"%s\"" % (row["migration source"], row["product id"]))

            except ValidationError as ex:
                self.import_result_messages.append("cannot save Product Migration for %s: %s" % (row["product id"],
                                                                                                 str(ex)))

            except Product.DoesNotExist:
                self.import_result_messages.append("Product %s not found in database, skip entry" % row["product id"])

