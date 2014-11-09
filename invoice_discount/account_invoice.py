# -*- coding: utf-8 -*-
import itertools
from lxml import etree

from openerp import models, fields, api, _
from openerp.exceptions import except_orm, Warning, RedirectWarning
from openerp.tools import float_compare
import openerp.addons.decimal_precision as dp


class account_invoice(models.Model):
    _inherit = "account.invoice"
    _description = "Invoice Discount"

    @api.one
    @api.depends('invoice_line.price_subtotal', 'tax_line.amount','discount')
    def _compute_amount(self):
        super(account_invoice,self)._compute_amount()

    @api.one
    @api.depends(
        'state', 'currency_id', 'discount', 'invoice_line.price_subtotal',
        'move_id.line_id.account_id.type',
        'move_id.line_id.amount_residual',
        'move_id.line_id.amount_residual_currency',
        'move_id.line_id.currency_id',
        'move_id.line_id.reconcile_partial_id.line_partial_ids.invoice.type',
    )
    def _compute_residual(self):
        super(account_invoice,self)._compute_residual()
    
    # Fields
    discount = fields.Float(string='Discount', digits=dp.get_precision('Account'),
        readonly=True, states={'draft': [('readonly', False)]}, default=0.0)
    amount_untaxed = fields.Float(string='Subtotal', digits=dp.get_precision('Account'),
        store=True, readonly=True, compute='_compute_amount', track_visibility='always')
    amount_tax = fields.Float(string='Tax', digits=dp.get_precision('Account'),
        store=True, readonly=True, compute='_compute_amount')
    amount_total = fields.Float(string='Total', digits=dp.get_precision('Account'),
        store=True, readonly=True, compute='_compute_amount')


    @api.model
    def _prepare_refund(self, invoice, date=None, period_id=None, description=None, journal_id=None):
        """ Prepare the dict of values to create the new refund from the invoice.
            This method may be overridden to implement custom
            refund generation (making sure to call super() to establish
            a clean extension chain).

            :param record invoice: invoice to refund
            :param string date: refund creation date from the wizard
            :param integer period_id: force account.period from the wizard
            :param string description: description of the refund from the wizard
            :param integer journal_id: account.journal from the wizard
            :return: dict of value to create() the refund
        """
        values = {}
        for field in ['name', 'discount', 'reference', 'comment', 'date_due', 'partner_id', 'company_id',
                'account_id', 'currency_id', 'payment_term', 'user_id', 'fiscal_position']:
            if invoice._fields[field].type == 'many2one':
                values[field] = invoice[field].id
            else:
                values[field] = invoice[field] or False

        values['invoice_line'] = self._refund_cleanup_lines(invoice.invoice_line)

        tax_lines = filter(lambda l: l.manual, invoice.tax_line)
        values['tax_line'] = self._refund_cleanup_lines(tax_lines)

        if journal_id:
            journal = self.env['account.journal'].browse(journal_id)
        elif invoice['type'] == 'in_invoice':
            journal = self.env['account.journal'].search([('type', '=', 'purchase_refund')], limit=1)
        else:
            journal = self.env['account.journal'].search([('type', '=', 'sale_refund')], limit=1)
        values['journal_id'] = journal.id

        values['type'] = TYPE2REFUND[invoice['type']]
        values['date_invoice'] = date or fields.Date.context_today(invoice)
        values['state'] = 'draft'
        values['number'] = False

        if period_id:
            values['period_id'] = period_id
        if description:
            values['name'] = description
        return values


class account_invoice_line(models.Model):
    _inherit = "account.invoice.line"
    _description = "Invoice Line with invoice discount"

    @api.one
    @api.depends('price_unit', 'discount', 'discount_1', 'discount_2', 'discount_3', 'invoice_line_tax_id', 'quantity',
        'product_id', 'invoice_id.partner_id', 'invoice_id.currency_id', 'invoice_id.discount')
    def _compute_price(self):
        price = self.price_unit * (1 - (self.discount or 0.0) / 100.0) *(1 - (self.discount_1 or 0.0) / 100.0)*(1 - (self.discount_2 or 0.0) / 100.0)*(1 - (self.discount_3 or 0.0) / 100.0)*(1 - (self.invoice_id.discount or 0.0) / 100.0)
        taxes = self.invoice_line_tax_id.compute_all(price, self.quantity, product=self.product_id, partner=self.invoice_id.partner_id)
        self.price_subtotal = taxes['total']
        self.price_subtotal_before_invoice_discount = self.price_unit * (1 - (self.discount or 0.0) / 100.0) *(1 - (self.discount_1 or 0.0) / 100.0)*(1 - (self.discount_2 or 0.0) / 100.0)*(1 - (self.discount_3 or 0.0) / 100.0)
        if self.invoice_id:
            self.price_subtotal = self.invoice_id.currency_id.round(self.price_subtotal)
            self.price_subtotal_before_invoice_discount = self.invoice_id.currency_id.round(self.price_subtotal_before_invoice_discount)

    price_subtotal_before_invoice_discount = fields.Float(string='Amount', digits= dp.get_precision('Account'),
        store=True, readonly=True, compute='_compute_price')
    price_subtotal = fields.Float(string='Amount', digits= dp.get_precision('Account'),
        store=True, readonly=True, compute='_compute_price')
    discount_1 = fields.Float(string='Discount (%)', digits= dp.get_precision('Discount'),
        default=0.0)
    discount_2 = fields.Float(string='Discount (%)', digits= dp.get_precision('Discount'),
        default=0.0)
    discount_3 = fields.Float(string='Discount (%)', digits= dp.get_precision('Discount'),
        default=0.0)

    @api.model
    def move_line_get(self, invoice_id):
        inv = self.env['account.invoice'].browse(invoice_id)
        currency = inv.currency_id.with_context(date=inv.date_invoice)
        company_currency = inv.company_id.currency_id

        res = []
        for line in inv.invoice_line:
            mres = self.move_line_get_item(line)
            mres['invl_id'] = line.id
            res.append(mres)
            tax_code_found = False
            taxes = line.invoice_line_tax_id.compute_all(
                (line.price_unit * (1 - (line.discount or 0.0) / 100.0) *(1 - (line.discount_1 or 0.0) / 100.0)*(1 - (line.discount_2 or 0.0) / 100.0)*(1 - (line.discount_3 or 0.0) / 100.0)*(1 - (line.invoice_id.discount or 0.0) / 100.0)),
                line.quantity, line.product_id, inv.partner_id)['taxes']
            for tax in taxes:
                if inv.type in ('out_invoice', 'in_invoice'):
                    tax_code_id = tax['base_code_id']
                    tax_amount = line.price_subtotal * tax['base_sign']
                else:
                    tax_code_id = tax['ref_base_code_id']
                    tax_amount = line.price_subtotal * tax['ref_base_sign']

                if tax_code_found:
                    if not tax_code_id:
                        continue
                    res.append(dict(mres))
                    res[-1]['price'] = 0.0
                    res[-1]['account_analytic_id'] = False
                elif not tax_code_id:
                    continue
                tax_code_found = True

                res[-1]['tax_code_id'] = tax_code_id
                res[-1]['tax_amount'] = currency.compute(tax_amount, company_currency)

        return res


class account_invoice_tax(models.Model):
    _inherit = "account.invoice.tax"

    @api.v8
    def compute(self, invoice):
        tax_grouped = {}
        currency = invoice.currency_id.with_context(date=invoice.date_invoice or fields.Date.context_today(invoice))
        company_currency = invoice.company_id.currency_id
        for line in invoice.invoice_line:
            taxes = line.invoice_line_tax_id.compute_all(
                (line.price_unit * (1 - (line.discount or 0.0) / 100.0) *(1 - (line.discount_1 or 0.0) / 100.0)*(1 - (line.discount_2 or 0.0) / 100.0)*(1 - (line.discount_3 or 0.0) / 100.0)*(1 - (line.invoice_id.discount or 0.0) / 100.0)),
                line.quantity, line.product_id, invoice.partner_id)['taxes']
            for tax in taxes:
                val = {
                    'invoice_id': invoice.id,
                    'name': tax['name'],
                    'amount': tax['amount'],
                    'manual': False,
                    'sequence': tax['sequence'],
                    'base': currency.round(tax['price_unit'] * line['quantity']),
                }
                if invoice.type in ('out_invoice','in_invoice'):
                    val['base_code_id'] = tax['base_code_id']
                    val['tax_code_id'] = tax['tax_code_id']
                    val['base_amount'] = currency.compute(val['base'] * tax['base_sign'], company_currency, round=False)
                    val['tax_amount'] = currency.compute(val['amount'] * tax['tax_sign'], company_currency, round=False)
                    val['account_id'] = tax['account_collected_id'] or line.account_id.id
                    val['account_analytic_id'] = tax['account_analytic_collected_id']
                else:
                    val['base_code_id'] = tax['ref_base_code_id']
                    val['tax_code_id'] = tax['ref_tax_code_id']
                    val['base_amount'] = currency.compute(val['base'] * tax['ref_base_sign'], company_currency, round=False)
                    val['tax_amount'] = currency.compute(val['amount'] * tax['ref_tax_sign'], company_currency, round=False)
                    val['account_id'] = tax['account_paid_id'] or line.account_id.id
                    val['account_analytic_id'] = tax['account_analytic_paid_id']

                key = (val['tax_code_id'], val['base_code_id'], val['account_id'], val['account_analytic_id'])
                if not key in tax_grouped:
                    tax_grouped[key] = val
                else:
                    tax_grouped[key]['base'] += val['base']
                    tax_grouped[key]['amount'] += val['amount']
                    tax_grouped[key]['base_amount'] += val['base_amount']
                    tax_grouped[key]['tax_amount'] += val['tax_amount']

        for t in tax_grouped.values():
            t['base'] = currency.round(t['base'])
            t['amount'] = currency.round(t['amount'])
            t['base_amount'] = currency.round(t['base_amount'])
            t['tax_amount'] = currency.round(t['tax_amount'])

        return tax_grouped

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
