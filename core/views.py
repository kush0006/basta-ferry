from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.generic import ListView, DetailView, View
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseBadRequest

import razorpay
import random
import string
import json

from .forms import CheckoutForm, CouponForm, RefundForm
from .models import Item, OrderItem, Order, BillingAddress, Payment, Coupon, Refund, Category

client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_ref_code():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=20))


class HomeView(ListView):
    template_name = "index.html"
    queryset = Item.objects.filter(is_active=True)
    context_object_name = 'items'


class ShopView(ListView):
    model = Item
    paginate_by = 6
    template_name = "shop.html"


class ItemDetailView(DetailView):
    model = Item
    template_name = "product-detail.html"


class CategoryView(View):
    def get(self, *args, **kwargs):
        category = Category.objects.get(slug=self.kwargs['slug'])
        item = Item.objects.filter(category=category, is_active=True)
        context = {'category': category, 'items': item}
        return render(self.request, 'category.html', context)


class OrderSummaryView(LoginRequiredMixin, View):
    def get(self, *args, **kwargs):
        try:
            order = Order.objects.get(user=self.request.user, ordered=False)
            return render(self.request, 'order_summary.html', {'object': order})
        except ObjectDoesNotExist:
            messages.error(self.request, "You do not have an active order")
            return redirect("/")


class CheckoutView(View):
    def get(self, *args, **kwargs):
        try:
            order = Order.objects.get(user=self.request.user, ordered=False)
            form = CheckoutForm()
            coupon_form = CouponForm()
            context = {
                'form': form,
                'order': order,
                'couponform': coupon_form,
                'DISPLAY_COUPON_FORM': True
            }
            return render(self.request, "checkout.html", context)
        except ObjectDoesNotExist:
            messages.error(self.request, "You do not have an active order")
            return redirect("core:checkout")

    def post(self, *args, **kwargs):
        form = CheckoutForm(self.request.POST or None)
        try:
            order = Order.objects.get(user=self.request.user, ordered=False)
            if form.is_valid():
                billing_address = BillingAddress(
                    user=self.request.user,
                    street_address=form.cleaned_data.get('street_address'),
                    apartment_address=form.cleaned_data.get('apartment_address'),
                    country=form.cleaned_data.get('country'),
                    zip=form.cleaned_data.get('zip')
                )
                billing_address.save()
                order.billing_address = billing_address
                order.save()
                return redirect('core:payment')
            messages.warning(self.request, "Checkout form is not valid.")
            return redirect("core:checkout")
        except ObjectDoesNotExist:
            return redirect("core:order-summary")


class PaymentView(View):
    def get(self, *args, **kwargs):
        try:
            order = Order.objects.get(user=self.request.user, ordered=False)
            if not order.billing_address:
                messages.warning(self.request, "Please complete your billing address.")
                return redirect("core:checkout")
            context = {
                'order': order,
                'DISPLAY_COUPON_FORM': False,
                'razorpay_key_id': settings.RAZORPAY_KEY_ID,
                'razorpay_amount': int(order.get_total() * 100)  # Razorpay uses paise
            }
            return render(self.request, "payment.html", context)
        except ObjectDoesNotExist:
            return redirect("/")


@csrf_exempt
def create_order(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        amount = data['amount'] * 100
        payment = client.order.create({
            "amount": amount,
            "currency": "INR",
            "payment_capture": 1
        })
        return JsonResponse(payment)


@csrf_exempt
def verify_payment(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        try:
            params_dict = {
                'razorpay_order_id': data['razorpay_order_id'],
                'razorpay_payment_id': data['razorpay_payment_id'],
                'razorpay_signature': data['razorpay_signature']
            }
            client.utility.verify_payment_signature(params_dict)

            order = Order.objects.get(user=request.user, ordered=False)
            payment = Payment.objects.create(
                user=request.user,
                razorpay_payment_id=data['razorpay_payment_id'],
                amount=order.get_total()
            )

            order.ordered = True
            order.payment = payment
            order.ref_code = create_ref_code()
            order.save()

            return JsonResponse({'status': 'Payment verified successfully'})
        except razorpay.errors.SignatureVerificationError:
            return HttpResponseBadRequest('Invalid Signature')


@login_required
def add_to_cart(request, slug):
    item = get_object_or_404(Item, slug=slug)
    order_item, created = OrderItem.objects.get_or_create(
        item=item, user=request.user, ordered=False)
    order_qs = Order.objects.filter(user=request.user, ordered=False)

    if order_qs.exists():
        order = order_qs[0]
        if order.items.filter(item__slug=item.slug).exists():
            order_item.quantity += 1
            order_item.save()
        else:
            order.items.add(order_item)
    else:
        order = Order.objects.create(user=request.user, ordered_date=timezone.now())
        order.items.add(order_item)

    messages.info(request, "Item added to cart")
    return redirect("core:order-summary")


@login_required
def remove_from_cart(request, slug):
    item = get_object_or_404(Item, slug=slug)
    order = Order.objects.filter(user=request.user, ordered=False).first()
    if order:
        if order.items.filter(item__slug=item.slug).exists():
            order_item = OrderItem.objects.get(item=item, user=request.user, ordered=False)
            order.items.remove(order_item)
            order_item.delete()
            messages.info(request, "Item removed from your cart")
            return redirect("core:order-summary")
    messages.info(request, "Item not in your cart")
    return redirect("core:product", slug=slug)


@login_required
def remove_single_item_from_cart(request, slug):
    item = get_object_or_404(Item, slug=slug)
    order = Order.objects.filter(user=request.user, ordered=False).first()
    if order:
        if order.items.filter(item__slug=item.slug).exists():
            order_item = OrderItem.objects.get(item=item, user=request.user, ordered=False)
            if order_item.quantity > 1:
                order_item.quantity -= 1
                order_item.save()
            else:
                order.items.remove(order_item)
            messages.info(request, "Item quantity updated")
            return redirect("core:order-summary")
    messages.info(request, "Item not in your cart")
    return redirect("core:product", slug=slug)


@login_required
def add_coupon(request):
    if request.method == "POST":
        form = CouponForm(request.POST)
        if form.is_valid():
            try:
                code = form.cleaned_data.get('code')
                order = Order.objects.get(user=request.user, ordered=False)
                coupon = Coupon.objects.get(code=code)
                order.coupon = coupon
                order.save()
                messages.success(request, "Coupon applied")
                return redirect("core:checkout")
            except ObjectDoesNotExist:
                messages.warning(request, "Invalid coupon")
                return redirect("core:checkout")


class RequestRefundView(View):
    def get(self, *args, **kwargs):
        form = RefundForm()
        return render(self.request, "request_refund.html", {'form': form})

    def post(self, *args, **kwargs):
        form = RefundForm(self.request.POST)
        if form.is_valid():
            ref_code = form.cleaned_data.get('ref_code')
            message = form.cleaned_data.get('message')
            email = form.cleaned_data.get('email')

            try:
                order = Order.objects.get(ref_code=ref_code)
                order.refund_requested = True
                order.save()

                refund = Refund(order=order, reason=message, email=email)
                refund.save()

                messages.info(self.request, "Refund request received.")
                return redirect("core:request-refund")
            except ObjectDoesNotExist:
                messages.warning(self.request, "Order not found.")
                return redirect("core:request-refund")
            
class AddCouponView(View):
    def get(self, request):
        return render(request, 'add_coupon.html')
    
def payment_page(request):
    return render(request, 'core/payment.html')

