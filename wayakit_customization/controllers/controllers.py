# from odoo import http
# import json
#
# from odoo.http import request
#
#
# class Odoo17Crack(http.Controller):
#     @http.route('/api/services/availableslots', methods=['POST'], type='json', csrf=False, auth="public")
#     def available_slots(self, **post):
#         # Extract data from the request body
#         service_id = 8
#
#         extras_ids = ["3680", "3681"]
#
#         appointment_type_id = request.env['appointment.type'].sudo().search([
#             ('id', '=', service_id),
#         ])
#         if appointment_type_id & appointment_type_id.product_id:
#
#             product_varients_ids = appointment_type_id.product_id.product_variant_ids.search([('id', 'in', extras_ids)])
#             totalduration = appointment_type_id.appointment_duration * 60
#             for product in product_varients_ids:
#                 totalduration += product.duration
#             totalduration = totalduration / 60
#             total = appointment_type_id.appointment_duration
#             appointment_type_id.appointment_duration = totalduration
#             tinmezone = 'Asia/Karachi'
#             slots = appointment_type_id._get_appointment_slots(
#                 tinmezone,
#             )
#             appointment_type_id.appointment_duration = total
#             # Structure the response
#             response = {
#                 "availableslots": slots
#             }
#
#             return response

import logging
from datetime import timedelta

from odoo import http, fields
from odoo.http import request
from odoo.addons.appointment.controllers.appointment import AppointmentController
from odoo.addons.base.models.ir_qweb import keep_query
import re

_logger = logging.getLogger(__name__)

# HOME Deep Cleaning Care (formerly Curtain and Furniture Care). Matched by
# ID first: the name was renamed once already and is translatable (Arabic site).
HOME_DEEP_CLEANING_TYPE_ID = 3
HOME_DEEP_CLEANING_NAMES = {"home deep cleaning care", "curtain and furniture care"}
MIN_PHONE_DIGITS = 8


def _is_home_deep_cleaning(appointment_type):
    """True for the HOME Deep Cleaning Care appointment type"""
    if not appointment_type:
        return False
    if appointment_type.id == HOME_DEEP_CLEANING_TYPE_ID:
        return True
    name = (appointment_type.with_context(lang='en_US').name or '').lower()
    return name in HOME_DEEP_CLEANING_NAMES


# Kitchen Steam Deep Cleaning: one appointment question per house group,
# each with its own price and duration. Matched by SKU, not by name: the
# product names carry brackets and "kitchen" also hits FP-RES chemicals.
KITCHEN_SERVICES = {
    'oasis': {'sku': 'FP-CUR-00026', 'hours': 1.5, 'areas': ('oasis', 'harbor')},
    'garden': {'sku': 'FP-CUR-00027', 'hours': 2.0, 'areas': ('garden', 'island', 'palm', 'nhc')},
}


def _kitchen_service(question_name):
    """KITCHEN_SERVICES entry for a kitchen question, None for any other question"""
    name = (question_name or '').lower()
    if 'kitchen' not in name:
        return None
    return KITCHEN_SERVICES['oasis' if 'oasis' in name else 'garden']


class CustomAppointmentController(AppointmentController):

    @http.route(['/appointment/<int:appointment_type_id>/submit'],
                type='http', auth="public", website=True, methods=["POST"])
    def appointment_form_submit(self, appointment_type_id, datetime_str, duration_str, name, phone, email, staff_user_id=None, available_resource_ids=None, asked_capacity=1,
                                guest_emails_str=None, **kwargs):
        kwargs.pop('duration_str', None)
        if len(re.sub(r'\D', '', phone or '')) < MIN_PHONE_DIGITS:
            return request.redirect('/appointment/%s?%s' % (
                appointment_type_id, keep_query('*', state='failed-phone')))
        appointment_type = request.env['appointment.type'].sudo().browse(appointment_type_id)
        if appointment_type and _is_home_deep_cleaning(appointment_type):
            # Kitchen keeps the full 2 h slot so availability is checked for
            # the longest possible visit; the real duration is set later
            if self._is_carpet_in_kwargs(kwargs) and not self._is_kitchen_in_kwargs(kwargs):
                duration_str = "0.5"
            else:
                duration_str = str(appointment_type.appointment_duration or "2.0")
        elif self._is_deep_cleaning_in_kwargs(kwargs) or (duration_str and float(duration_str) >= 2.0):
            duration_str = "2.0"
        return super().appointment_form_submit(
            appointment_type_id, datetime_str, duration_str, name, phone, email,
            staff_user_id=staff_user_id, available_resource_ids=available_resource_ids,
            asked_capacity=asked_capacity, guest_emails_str=guest_emails_str, **kwargs
        )

    def _is_deep_cleaning_in_kwargs(self, kwargs):
        """Check if any answer passed in form kwargs indicates deep cleaning."""
        try:
            answer_ids = []
            for k, v in kwargs.items():
                if not k.startswith('question_') or not v:
                    continue
                match = re.match(r"\bquestion_([0-9]+)_answer_([0-9]+)\b", k)
                if match:
                    answer_ids.append(int(match.group(2)))
                elif isinstance(v, list):
                    answer_ids.extend([int(x) for x in v if str(x).isdigit()])
                elif str(v).isdigit():
                    answer_ids.append(int(v))
            if answer_ids:
                answers = request.env['appointment.answer'].sudo().browse(answer_ids)
                for ans in answers:
                    if ans.name and 'deep clean' in ans.name.lower():
                        return True
        except Exception:
            _logger.exception("Failed to check deep-cleaning in kwargs")
        return False

    def _is_kitchen_in_kwargs(self, kwargs):
        """True if any kitchen question was answered with a quantity > 0."""
        try:
            for k, v in kwargs.items():
                match = re.match(r"^question_([0-9]+)$", k)
                if not match or not str(v).isdigit():
                    continue
                question = request.env['appointment.question'].sudo().with_context(lang='en_US').browse(int(match.group(1)))
                answer = request.env['appointment.answer'].sudo().browse(int(v))
                quantity_match = re.match(r'^(\d+)', answer.name or '')
                if _kitchen_service(question.name) and quantity_match and int(quantity_match.group(1)) > 0:
                    return True
        except Exception:
            _logger.exception("Failed to check kitchen in kwargs")
        return False

    def _is_carpet_in_kwargs(self, kwargs):
        """Check if any answer passed in form kwargs indicates a carpet service selection."""
        try:
            for k, v in kwargs.items():
                if not k.startswith('question_') or not v:
                    continue

                # Checkbox match: question_<qid>_answer_<aid>
                match_cb = re.match(r"\bquestion_([0-9]+)_answer_([0-9]+)\b", k)
                if match_cb:
                    qid = int(match_cb.group(1))
                    aid = int(match_cb.group(2))
                    question = request.env['appointment.question'].sudo().browse(qid)
                    answer = request.env['appointment.answer'].sudo().browse(aid)
                    if (answer.name and 'carpet' in answer.name.lower()) or \
                       (question.name and 'carpet' in question.name.lower()):
                        return True
                    continue

                # Select / Radio match: question_<qid>
                match_q = re.match(r"\bquestion_([0-9]+)\b", k)
                if match_q:
                    qid = int(match_q.group(1))
                    question = request.env['appointment.question'].sudo().browse(qid)
                    answer_ids = []
                    if isinstance(v, list):
                        answer_ids.extend([int(x) for x in v if str(x).isdigit()])
                    elif str(v).isdigit():
                        answer_ids.append(int(v))

                    for aid in answer_ids:
                        answer = request.env['appointment.answer'].sudo().browse(aid)
                        if not answer or not answer.name:
                            continue
                        ans_name = answer.name.strip()
                        if 'carpet' in ans_name.lower():
                            return True
                        if question.name and 'carpet' in question.name.lower():
                            qty_match = re.match(r'^(\d+)', ans_name)
                            if qty_match:
                                if int(qty_match.group(1)) > 0:
                                    return True
                            elif ans_name.lower() not in ['0', 'no', 'none', 'false', '']:
                                return True
        except Exception:
            _logger.exception("Failed to check carpet in kwargs")
        return False

    def _handle_appointment_form_submission(
            self, appointment_type,
            date_start, date_end, duration,
            description, answer_input_values, name, customer, appointment_invite, guests=None,
            staff_user=None, asked_capacity=1, booking_line_values=None
    ):
        # A kitchen question only applies to its own house group; the form
        # hides the other one, so this only trips on a tampered POST
        kitchen_area, kitchen_services = self._get_kitchen_booking(answer_input_values)
        if any(kitchen_area not in service['areas'] for service in kitchen_services):
            return request.redirect('/appointment/%s?%s' % (
                appointment_type.id, keep_query('*', state='failed-kitchen-area')))

        # Create customer contact from form data and sales order
        sale_order = False
        customer_partner = self._create_or_update_customer_partner(name, description)
        # Resolve the selected service product once; reused for the sale
        # order below
        product = self._get_selected_service_product(appointment_type, answer_input_values)

        if customer_partner:
            # Check appointment type and handle accordingly
            if _is_home_deep_cleaning(appointment_type):
                sale_order = self._create_curtain_furniture_sale_order(
                    appointment_type,
                    customer_partner,
                    answer_input_values
                )
            elif product:
                # For Car Wash Care and other types, use the existing logic
                sale_order = self._create_appointment_sale_order(
                    appointment_type,
                    customer_partner,  # Use the created/updated customer
                    product
                )

        # "Deep cleaning" service selection always forces a fixed 2-hour
        # slot, regardless of the appointment type's configured duration.
        # Keyed off the "Type of service" answer text directly (not the
        # matched product), so it doesn't depend on product naming or
        # default_code being set up correctly.
        if self._is_deep_cleaning_selected(answer_input_values):
            duration = 2
            date_end = date_start + timedelta(hours=2)

        # In HOME Deep Cleaning Care (formerly Curtain and Furniture Care) appointment, if customer selected any service type includes carpet, duration should be 30 min (0.5 hour)
        if appointment_type and _is_home_deep_cleaning(appointment_type):
            if self._is_carpet_selected(answer_input_values):
                duration = 0.5
                date_end = date_start + timedelta(minutes=30)

        # Kitchen deep cleaning is done on-site by the house group's own
        # timing and wins over the 30-min carpet pickup
        if kitchen_services:
            duration = max(service['hours'] for service in kitchen_services)
            date_end = date_start + timedelta(hours=duration)

        # Call original method to create calendar event
        result = super()._handle_appointment_form_submission(
            appointment_type, date_start, date_end, duration,
            description, answer_input_values, name, customer, appointment_invite, guests,
            staff_user, asked_capacity, booking_line_values
        )

        # Link sale order to event if created
        if sale_order:
            event_token = result.location.split('/')[-1].split('?')[0]
            event = request.env['calendar.event'].sudo().search(
                [('access_token', '=', event_token)],
                limit=1
            )
            if event:
                event.sale_order_id = sale_order.id

        return result

    def _get_kitchen_booking(self, answer_input_values):
        """Return (area, kitchen services picked with quantity > 0).
        Read in English: the Arabic form carries translated labels."""
        area, services = '', []
        for answer in answer_input_values or []:
            if not answer.get('value_answer_id'):
                continue
            question = request.env['appointment.question'].sudo().with_context(lang='en_US').browse(answer['question_id'])
            answer_name = request.env['appointment.answer'].sudo().with_context(lang='en_US').browse(answer['value_answer_id']).name or ''
            if (question.name or '').strip().lower() == 'area':
                area = answer_name.strip().lower()
                continue
            service = _kitchen_service(question.name)
            quantity_match = re.match(r'^(\d+)', answer_name)
            if service and quantity_match and int(quantity_match.group(1)) > 0:
                services.append(service)
        return area, services

    def _is_deep_cleaning_selected(self, answer_input_values):
        """True if the customer picked a 'deep cleaning' Type of service
        option, regardless of appointment type or matched product."""
        try:
            _, selected_answer = self._find_service_selection(answer_input_values)
            if not selected_answer or not selected_answer.name:
                return False
            return 'deep clean' in selected_answer.name.lower()
        except Exception:
            # Never block a booking on this check; fall back to the
            # appointment type's default duration.
            _logger.exception("Failed to check deep-cleaning selection")
            return False

    def _is_carpet_selected(self, answer_input_values):
        """True if the customer picked any service type that includes carpet
        in HOME Deep Cleaning Care (formerly Curtain and Furniture Care)."""
        if not answer_input_values:
            return False
        try:
            questions = request.env['appointment.question'].sudo().browse(
                list(dict.fromkeys(a['question_id'] for a in answer_input_values if 'question_id' in a))
            )
            questions_by_id = {q.id: q for q in questions}

            for answer_val in answer_input_values:
                qid = answer_val.get('question_id')
                aid = answer_val.get('value_answer_id')
                if not qid or not aid:
                    continue
                question = questions_by_id.get(qid)
                answer_rec = request.env['appointment.answer'].sudo().browse(aid)
                if not answer_rec or not answer_rec.name:
                    continue

                ans_name = answer_rec.name.strip()
                # If answer itself mentions carpet
                if 'carpet' in ans_name.lower():
                    return True

                # If question mentions carpet
                if question and question.name and 'carpet' in question.name.lower():
                    qty_match = re.match(r'^(\d+)', ans_name)
                    if qty_match:
                        if int(qty_match.group(1)) > 0:
                            return True
                    elif ans_name.lower() not in ['0', 'no', 'none', 'false', '']:
                        return True
        except Exception:
            _logger.exception("Failed to check carpet selection")
            return False

    def _create_or_update_customer_partner(self, name, description):
        """Create or update customer partner from form data"""
        try:
            # Extract email and phone from description (HTML format)
            email = phone = None

            # Parse the description HTML to extract email and phone
            if description and '<ul>' in description:
                # Example description format:
                # <ul><li>Phone: +1234567890</li><li>Email: customer@example.com</li></ul>
                import re
                phone_match = re.search(r'Phone:? ([^<]+)', description)
                email_match = re.search(r'Email:? ([^<]+)', description)

                if phone_match:
                    phone = phone_match.group(1).strip()
                if email_match:
                    email = email_match.group(1).strip()

            # Search for existing partner by email or phone
            partner_obj = request.env['res.partner'].sudo()
            existing_partner = False

            if email:
                existing_partner = partner_obj.search([
                    '|', ('email', '=ilike', email), ('email_normalized', '=ilike', email)
                ], limit=1)

            if not existing_partner and phone:
                existing_partner = partner_obj.search([
                    ('phone', '=ilike', phone),
                    ('mobile', '=ilike', phone)
                ], limit=1, order='id desc')

            partner_vals = {
                'name': name,
                'phone': phone,
                'email': email,
            }


            if existing_partner:
                # Update existing partner
                existing_partner.write(partner_vals)
                return existing_partner
            else:
                # Create new partner
                return partner_obj.create(partner_vals)

        except Exception as e:
            # Fallback: create partner with just the name
            return request.env['res.partner'].sudo().create({'name': name})

    def _get_selected_service_product(self, appointment_type, answer_input_values):
        """Return the service product variant selected in the booking form, if any"""
        if appointment_type.name.lower() != "car wash care":
            return None

        try:
            service_question, selected_answer = self._find_service_selection(answer_input_values)
            if not (service_question and selected_answer and selected_answer.name):
                return None
            vehicle_type_answer = self._find_vehicle_type_selection(answer_input_values)
            return self._find_matching_product(selected_answer.name, vehicle_type_answer)
        except Exception:
            # Never block a booking on the custom product matching; fall back
            # to the standard appointment duration / no sale order
            _logger.exception(
                "Failed to resolve service product for appointment type %s",
                appointment_type.name,
            )
            return None

    def _create_appointment_sale_order(self, appointment_type, customer_partner, product):
        """Create a sale order from appointment data with proper product matching"""
        # Only process if this is a Car Wash Care appointment
        if appointment_type.name.lower() != "car wash care":
            return False

        # Create the sales order with the customer partner (not logged-in user)
        order_vals = {
            'partner_id': customer_partner.id,  # Use the customer from form
            'user_id': request.env.user.id,  # Keep logged-in user as salesperson
            'date_order': fields.Datetime.now(),
            'origin': f"Appointment: {appointment_type.name}",
            'order_line': [(0, 0, {
                'product_id': product.id,
                'name': product.name,
                'product_uom_qty': 1,
                'price_unit': product.list_price,
            })]
        }

        return request.env['sale.order'].sudo().create(order_vals)

    def _create_curtain_furniture_sale_order(self, appointment_type, customer_partner, answer_input_values):
        """Create a sale order for HOME Deep Cleaning Care (formerly Curtain and Furniture Care) appointment type"""
        # Only process if this is a HOME Deep Cleaning Care (formerly Curtain and Furniture Care) appointment
        if not _is_home_deep_cleaning(appointment_type):
            return False

        # Extract product quantities from the answers
        product_quantities = self._extract_curtain_furniture_quantities(answer_input_values)

        if not product_quantities:
            print("No product quantities found for Curtain and Furniture Care")
            return False

        # Create order lines based on the extracted quantities
        order_lines = []
        for question_text, quantity in product_quantities.items():
            if quantity > 0:
                product = self._find_curtain_furniture_product(question_text)
                if product:
                    order_lines.append((0, 0, {
                        'product_id': product.id,
                        'name': product.name,
                        'product_uom_qty': quantity,
                        'price_unit': product.list_price,
                    }))
                    print(f"Added product: {product.name}, Quantity: {quantity}")
                else:
                    print(f"Product not found for: {question_text}")

        if not order_lines:
            print("No order lines created for Curtain and Furniture Care")
            return False

        # Create the sales order
        order_vals = {
            'partner_id': customer_partner.id,
            'user_id': request.env.user.id,
            'date_order': fields.Datetime.now(),
            'origin': f"Appointment: {appointment_type.name}",
            'order_line': order_lines
        }

        return request.env['sale.order'].sudo().create(order_vals)

    def _extract_curtain_furniture_quantities(self, answer_input_values):
        """Extract product quantities from HOME Deep Cleaning Care (formerly Curtain and Furniture Care) appointment answers"""
        product_quantities = {}

        for answer in answer_input_values:
            question = request.env['appointment.question'].browse(answer['question_id'])

            # Skip basic info questions (name, email, phone, area, unit number)
            basic_info_fields = ['full name', 'email', 'phone number', 'area', 'unit number']
            if question.name.lower() in basic_info_fields:
                continue

            # For selection questions with quantities (0-10)
            if question.question_type == 'select' and 'value_answer_id' in answer:
                answer_record = request.env['appointment.answer'].browse(answer['value_answer_id'])
                if answer_record:
                    # Extract quantity from answer (e.g., "6" from "6")
                    quantity_match = re.match(r'^(\d+)', answer_record.name)
                    if quantity_match:
                        quantity = int(quantity_match.group(1))
                        # Use the question text to identify the product, in
                        # English so the Arabic form maps to the same product
                        question_text = question.with_context(lang='en_US').name

                        if question_text and quantity > 0:
                            product_quantities[question_text] = quantity
                            print(f"Extracted product: {question_text}, Quantity: {quantity}")

        return product_quantities

    def _find_curtain_furniture_product(self, question_text):
        """Find matching product for HOME Deep Cleaning Care (formerly Curtain and Furniture Care) items based on question text"""
        kitchen = _kitchen_service(question_text)
        if kitchen:
            return request.env['product.product'].sudo().search([('default_code', '=', kitchen['sku'])], limit=1)

        # Map form question text to search terms for products
        product_search_terms = {
            "Curtain set [SAR 350 VAT included]": "Curtain set",
            "Dining chair [SAR 62 VAT included]": "Dining Chair",
            "Blue Chair Oasis [SAR 56.15 VAT included]": "Blue Dining chair",
            "Small Sofa [SAR 350 VAT included]": "Small Sofa",
            "Big Sofa [SAR 420 VAT included]": "Big Sofa",
            "Mattresses [SAR 336.89 VAT included]": "Mattress",
            "Small Carpet up to 2 sqm [SAR 40 VAT included]": "Small up to 2 sqm",
            "Medium Carpet 2.1 to 6 sqm [SAR 160 VAT included]": "Medium 2.1-6 sqm",
            "Big Carpet 7 to 20 sqm [SAR 240 VAT included]": "Big 7-20 sqm",
            "Extra Big Carpet 17-20 sqm [SAR 380.01 VAT included]": "Extra Big",
            "Extra Big Carpet 21-25 sqm [SAR 440.00 VAT included]": "Extra Big 21-25 sqm",
        }
        # Get the search term for this question
        search_term = product_search_terms.get(question_text)

        if not search_term:
            print(f"No search term found for question: {question_text}")
            return None

        # Search for the product using ilike for case-insensitive search
        domain = [
            ('name', 'ilike', search_term),
            ('type', '=', 'service')
        ]

        product = request.env['product.product'].sudo().search(domain, limit=1)
        print(f"Searching for product with term: '{search_term}', Found: {product.name if product else 'None'}")
        return product

    def _find_service_selection(self, answer_input_values):
        """Find the service selection question and answer"""
        if not answer_input_values:
            return (None, None)
        # Load every asked question in one query; browsing them one by one
        # below would trigger a separate read per answer. Map by id because
        # several answers (checkbox questions) can share the same question.
        questions = request.env['appointment.question'].browse(
            list(dict.fromkeys(answer['question_id'] for answer in answer_input_values))
        )
        questions_by_id = {question.id: question for question in questions}
        for answer in answer_input_values:
            question = questions_by_id.get(answer['question_id'])
            if question and 'service' in question.name.lower() and question.question_type == 'select':
                return (
                    question,
                    request.env['appointment.answer'].browse(answer['value_answer_id'])
                )
        return (None, None)

    def _find_vehicle_type_selection(self, answer_input_values):
        """Find the answer to the dedicated 'Type of car' question (Sedan/SUV/etc.)

        The service-selection answer text can mention multiple vehicle types
        at once for pricing display (e.g. "...(Sedan SAR 260, SUV SAR 330)"),
        so the vehicle type must be read from its own question instead.
        """
        if not answer_input_values:
            return None
        questions = request.env['appointment.question'].browse(
            list(dict.fromkeys(answer['question_id'] for answer in answer_input_values))
        )
        questions_by_id = {question.id: question for question in questions}
        for answer in answer_input_values:
            question = questions_by_id.get(answer['question_id'])
            if question and 'type of car' in question.name.lower() and question.question_type == 'select':
                answer_record = request.env['appointment.answer'].browse(answer['value_answer_id'])
                return answer_record.name if answer_record else None
        return None

    def _find_matching_product(self, answer_name, vehicle_type_answer=None):
        """Match the appointment answer to an existing product"""
        if not answer_name:
            return None

        # Only process if this is a Car Wash Care appointment
        # Clean and parse the answer text
        service_type = self._extract_service_type(answer_name)
        vehicle_type = self._extract_vehicle_type(vehicle_type_answer or answer_name)

        if not service_type or not vehicle_type:
            return None

        # Search for matching product
        domain = [
            ('name', 'ilike', vehicle_type),
            ('name', 'ilike', service_type),
            ('type', '=', 'service')
        ]
        if service_type != 'Deep cleaning':
            domain.append(('name', 'not ilike', 'Deep cleaning'))

        return request.env['product.product'].sudo().search(domain, limit=1)

    def _extract_service_type(self, answer_name):
        """Extract service type from answer (e.g., 'Exterior', 'Interior')"""
        answer_name = answer_name.lower()
        if 'deep' in answer_name:
            return 'Deep cleaning'
        elif 'exterior' in answer_name and 'interior' in answer_name:
            return 'Exterior & Interior'
        elif 'exterior' in answer_name:
            return 'Exterior'
        elif 'interior' in answer_name:
            return 'Interior'
        return None

    def _extract_vehicle_type(self, answer_name):
        """Extract vehicle type from answer (e.g., 'SUV', 'Sedan')"""
        vehicle_types = ['suv', 'sedan', 'golfcart', 'motorcycle']
        answer_name = answer_name.lower()
        for v_type in vehicle_types:
            if v_type in answer_name:
                return v_type.capitalize()
        return None