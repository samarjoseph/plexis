
import threading
from flask import Flask, render_template, request, redirect, session, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import random, smtplib
from email.message import EmailMessage
import io, os
from reportlab.lib.enums import TA_RIGHT, TA_CENTER ,TA_LEFT
from apscheduler.schedulers.background import BackgroundScheduler
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle
import stripe
from datetime import datetime
from flask import request
import os




app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")
app.permanent_session_lifetime = timedelta(days=7)


stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
APP_EMAIL = os.getenv("APP_EMAIL_USE")
APP_PASSWORD = os.getenv("APP_SECRET_EMAIL_KEY")


# ---------------- DATABASE ----------------



app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# PostgreSQL connection (Render safe)
uri = os.getenv("DATABASE_URL")

if uri:
    # Render fix (important)
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)

    app.config['SQLALCHEMY_DATABASE_URI'] = uri
else:
    # fallback for local testing
    db_url = os.getenv("DATABASE_URL")

    if db_url:
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://")

    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)



BASE_URL = os.getenv("BASE_URL")
# ---------------- MODELS ----------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)
    email = db.Column(db.String(100))
    password = db.Column(db.String(200))

    plan = db.Column(db.String(20), default="free")
    trial_start = db.Column(db.DateTime)
    stripe_subscription_id = db.Column(db.String(200))

    invoices_used = db.Column(db.Integer, default=0)
    emails_sent = db.Column(db.Integer, default=0)

    reset_code = db.Column(db.String(10), nullable=True)
    reset_expiry = db.Column(db.DateTime)
    plan_start = db.Column(db.DateTime)
    last_reset = db.Column(db.DateTime)


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100))


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    client_id = db.Column(db.Integer)
    title = db.Column(db.String(100))
    amount = db.Column(db.Float)
    currency = db.Column(db.String(10))
    status = db.Column(db.String(20), default="pending")
    date = db.Column(db.DateTime, default=datetime.utcnow)
    invoice_id = db.Column(db.String(20))
    due_date = db.Column(db.DateTime, nullable=True)
    overdue_notified = db.Column(db.Boolean, default=False)
    overdue_dismissed = db.Column(db.Boolean, default=False)

with app.app_context():
    db.create_all()

# ---------------- PLAN SYSTEM ----------------
def check_trial(user):
    if not user:
        return

    if user.trial_start and user.plan == "pro":
        if datetime.utcnow() > user.trial_start + timedelta(days=10):
            user.plan = "free"
            db.session.commit()

def get_limits(plan):
    return {
        "free": {"emails": 6, "invoices": 6},
        "plus": {"emails": 15, "invoices": 15},
        "pro": {"emails": 999999, "invoices": 999999}
    }[plan]

def can_use_feature(user, feature):
    check_trial(user)
    limits = get_limits(user.plan)

    if feature == "email":
        return user.emails_sent < limits["emails"]

    if feature == "invoice":
        return user.invoices_used < limits["invoices"]

    if feature == "overdue":
        return user.plan == "pro"

    return True

# ---------------- PDF SETUP ----------------
font_path = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
pdfmetrics.registerFont(TTFont('DejaVu', font_path))

def generate_pdf(project, client, user):
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )

    BRAND_BLUE = colors.HexColor("#1E40AF")  
    BOX_LIGHT_BLUE = colors.HexColor("#EFF6FF")   
    BOX_MEDIUM_BLUE = colors.HexColor("#DBEAFE")  
    BOX_GREY = colors.HexColor("#F1F5F9")    
    TEXT_DARK = colors.HexColor("#1E293B")   
    TEXT_MUTED = colors.HexColor("#64748B")  

    style_brand = ParagraphStyle(name="Brand", fontSize=16, fontName="Helvetica-Bold", textColor=BRAND_BLUE)
    style_label = ParagraphStyle(name="Label", fontSize=7, fontName="Helvetica-Bold", textColor=BRAND_BLUE)
    style_value = ParagraphStyle(name="Value", fontSize=10, fontName="Helvetica", textColor=TEXT_DARK)
    style_notice = ParagraphStyle(name="Notice", fontSize=8, fontName="Helvetica-Bold", textColor=BRAND_BLUE, alignment=TA_RIGHT)
    style_paid_status = ParagraphStyle(name="PaidStatus", fontSize=10, fontName="Helvetica-Bold", textColor=TEXT_DARK)
    style_status_col = ParagraphStyle(name="StatusCol", fontSize=10, fontName="Helvetica-Bold", textColor=TEXT_MUTED)
    style_amount = ParagraphStyle(name="Amount", fontSize=18, fontName="Helvetica-Bold", textColor=BRAND_BLUE, alignment=TA_RIGHT)

    elements = []

    # ================= HEADER =================
    header = Table([
        [
            Paragraph("PLEXIS", style_brand),
            Paragraph("INVOICE", ParagraphStyle(name="T", fontSize=20, fontName="Helvetica-Bold", alignment=TA_RIGHT, textColor=TEXT_DARK))
        ]
    ], colWidths=[250, 260])

    elements.append(header)
    elements.append(Spacer(1, 25))

    # ================= INVOICE BOX =================
    inv_data = [
        [Paragraph("INVOICE ID", style_label), Paragraph("DATE", style_label), Paragraph("STATUS", style_label)],
        [
            Paragraph(project.invoice_id, style_value),
            Paragraph(project.date.strftime("%d %b %Y"), style_value),
            Paragraph(project.status.upper(), style_paid_status)
        ]
    ]

    inv_box = Table(inv_data, colWidths=[170, 170, 170])
    inv_box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BOX_LIGHT_BLUE),
        ('BOX', (0,0), (-1,-1), 1, BRAND_BLUE),
        ('TOPPADDING', (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
    ]))

    elements.append(inv_box)
    elements.append(Spacer(1, 15))

    # ================= CLIENT BOX (FIXED HERE) =================
    client_data = [
        [Paragraph("BILLED TO", style_label)],
        [Paragraph(f"<b>{client.name}</b>", style_value)],
        [Paragraph(client.email, style_value)]
    ]

    client_box = Table(client_data, colWidths=[510])
    client_box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BOX_GREY),
        ('BOX', (0,0), (-1,-1), 1, BRAND_BLUE),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
    ]))

    elements.append(client_box)
    elements.append(Spacer(1, 30))

    # ================= ITEMS =================
    items_data = [
        [Paragraph("DESCRIPTION", style_label), Paragraph("STATUS", style_label), Paragraph("AMOUNT", style_label)],
        [
            Paragraph(project.title, style_value),
            Paragraph(project.status.upper(), style_status_col),
            Paragraph(f"<b>{project.currency} {project.amount}</b>", style_value)
        ]
    ]

    items_table = Table(items_data, colWidths=[300, 100, 110])
    items_table.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,0), 1, BOX_MEDIUM_BLUE),
        ('LINEBELOW', (0,1), (-1,1), 1.5, BRAND_BLUE),
        ('ALIGN', (2,0), (2,-1), 'RIGHT'),
    ]))

    elements.append(items_table)
    elements.append(Spacer(1, 30))

    # ================= TOTAL =================
    total_data = [
        [Paragraph("TOTAL AMOUNT DUE", style_label), Paragraph(f"{project.currency} {project.amount}", style_amount)],
        ["", Paragraph("IMMEDIATE PAYMENT REQUIRED", style_notice)]
    ]

    total_box = Table(total_data, colWidths=[330, 180])
    total_box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BOX_MEDIUM_BLUE),
        ('BOX', (0,0), (-1,-1), 1, BRAND_BLUE),
    ]))

    elements.append(total_box)

    # ================= FOOTER =================
    elements.append(Spacer(1, 60))

    footer_text = f"""
    Questions? Contact: {user.email}<br/>
    Generated by Plexis • Professional • Trusted
    """

    footer = Paragraph(
        footer_text,
        ParagraphStyle(name="F", alignment=TA_CENTER, fontSize=8, textColor=TEXT_MUTED)
    )

    elements.append(footer)

    doc.build(elements)
    buffer.seek(0)
    return buffer
# ---------------- AUTH ----------------
@app.route("/home")
def landing():
    # if user already logged in → skip landing
    if 'user_id' in session:
        return redirect("/dashboard")

    return render_template("index.html")


@app.route("/start")
def start():
    if 'user_id' in session:
        return redirect("/dashboard")
    return redirect("/home")


@app.route("/", methods=["GET","POST"])
def login():
    if 'user_id' in session:
        return redirect("/dashboard")

    if request.method == "POST":
        user = User.query.filter_by(name=request.form.get("username")).first()

        if user and check_password_hash(user.password, request.form.get("password")):
            session['user_id'] = user.id
            session.permanent = True
            return redirect("/dashboard")

        flash("invalid login details or create your account first")

    return render_template("login.html")

@app.route("/register", methods=["GET","POST"])
def register():

        if request.method == "POST":

            name = request.form.get("name")
            email = request.form.get("email")
            password = request.form.get("password")

            # ✅ CHECK USERNAME
            if User.query.filter_by(name=name).first():
                flash("Username already exists!")
                return redirect("/register")

            # ✅ CHECK EMAIL LIMIT (2 accounts)
            if User.query.filter_by(email=email).count() >= 2:
                flash("Max 2 accounts allowed for this email")
                return redirect("/register")

            user = User(
                name=name,
                email=email,
                password=generate_password_hash(password),
                plan="free",
                trial_start=None
            )

            db.session.add(user)
            db.session.commit()

            session['user_id'] = user.id
            return redirect("/dashboard")

        return render_template("register.html")

@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email").strip()

        user = User.query.filter_by(email=email).first()

        if not user:
            flash("Email not found ❌")
            return redirect("/forgot")

        code = str(random.randint(100000, 999999))
        user.reset_code = code
        user.reset_expiry = datetime.utcnow() + timedelta(minutes=5)
        user.reset_code = code
        db.session.commit()

        session['reset_email'] = email  # 🔥 VERY IMPORTANT

        # send email
        msg = EmailMessage()
        msg['Subject'] = "Reset Code"
        msg['From'] = APP_EMAIL
        msg['To'] = email
        msg.set_content(f"Your reset code is: {code}")

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(APP_EMAIL, APP_PASSWORD)
        server.send_message(msg)
        server.quit()

        flash("Code sent to email ")
        return redirect("/reset")

    return render_template("forgot.html")


@app.route("/reset", methods=["GET", "POST"])
def reset():
    if request.method == "POST":
        code = request.form.get("code")
        password = request.form.get("password")

        email = session.get("reset_email")  # 🔥 NOT from form

        if not email:
            flash("Session expired ")
            return redirect("/forgot")

        user = User.query.filter_by(email=email).first()

        if not user:
            flash("User not found ❌")
            return redirect("/forgot")

        if user.reset_code != code:
            flash("Invalid code ❌")
            return redirect("/reset")

        user.password = generate_password_hash(password)
        user.reset_code = None
        db.session.commit()

        session.pop('reset_email', None)

        flash("Password reset successful ")
        return redirect("/")
    
        if datetime.utcnow() > user.reset_expiry:
            flash("Code expired ⏱️")
            return redirect("/forgot")

    return render_template("reset.html")
@app.route("/update_project/<int:id>", methods=["POST"])
def update_project(id):
    if 'user_id' not in session:
        return redirect("/")

    user = User.query.get(session.get('user_id'))

    if not user:
        session.clear()
        return redirect("/")

    project = Project.query.get(id)

    if not project or project.user_id != user.id:
        flash("Unauthorized ❌")
        return redirect("/clients")

    client = Client.query.get(project.client_id)

    # 🔥 UPDATE PROJECT FIELDS
    project.title = request.form.get("title")
    project.amount = float(request.form.get("amount"))
    project.status = request.form.get("status")

    # 🔥 UPDATE CLIENT FIELDS
    if client:
        client.name = request.form.get("client_name")
        client.email = request.form.get("email") or client.email

    db.session.commit()

    flash("✅ Project & Client Updated Successfully")
    return redirect("/clients")
# ---------------- START TRIAL ----------------
@app.route("/start_trial")
def start_trial():
    user = User.query.get(session['user_id'])

    if user.trial_start:
        flash("Trial already used")
        return redirect("/plans")

    user.plan = "pro"
    user.trial_start = datetime.utcnow()
    db.session.commit()

    flash(" 10-day Pro trial started!")
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/home")

@app.route("/delete_account")
def delete_account():
    user = User.query.get(session['user_id'])
    Client.query.filter_by(user_id=user.id).delete()
    Project.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    session.clear()
    return redirect("/")

# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if 'user_id' not in session:
        return redirect("/")

    user = User.query.get(session.get('user_id'))

    if not user:
        session.clear()
        return redirect("/")

    check_trial(user)

    total_clients = Client.query.filter_by(user_id=user.id).count()
    pending_projects = Project.query.filter_by(user_id=user.id, status="pending").count()
    pending_amount = db.session.query(db.func.sum(Project.amount)) \
                      .filter_by(user_id=user.id, status="pending").scalar() or 0

    overdue_projects = Project.query.filter(
        Project.user_id == user.id,
        Project.overdue_notified == True,
        Project.overdue_dismissed == False
    ).all()


    return render_template(
        "dashboard.html",
        user=user,
        total_clients=total_clients,
        pending_projects=pending_projects,
        pending_amount=pending_amount,
        overdue_projects=overdue_projects   # 🔥 PASS TO HTML
    )



# ---------------- CLIENTS ----------------
@app.route("/clients")
def clients():
    user = User.query.get(session['user_id'])

    projects = Project.query.filter_by(user_id=user.id).all()
    data = []

    for p in projects:
        c = Client.query.get(p.client_id)
        data.append({
                "id": p.id,   # 🔥 IMPORTANT CHANGE
                "client_name": c.name if c else "Unknown",
                "email": c.email if c else "",
                "title": p.title,
                "amount": p.amount,
                "currency": p.currency,
                "status": p.status,
                "date": p.date.strftime('%d %b %Y'),
                "due_date": p.due_date,                 # ✅ ADD
                "overdue_dismissed": p.overdue_dismissed  # ✅ ADD
})

    return render_template("clients.html", projects=data , user=user)

# ---------------- OVERDUE ----------------

# ---------------- PROJECT ----------------
@app.route("/add_project", methods=["POST"])
def add_project():
    user = User.query.get(session['user_id'])

    if not can_use_feature(user, "invoice"):
        flash(" Upgrade to Plus or Pro to create more invoices")
        return redirect("/dashboard")

    client = Client(
        user_id=user.id,
        name=request.form.get("client_name"),
        email=request.form.get("email")
    )
    db.session.add(client)
    db.session.commit()

    project = Project(
        user_id=user.id,
        client_id=client.id,
        title=request.form.get("title"),
        amount=float(request.form.get("amount")),
        currency=request.form.get("currency"),
        invoice_id="INV"+str(random.randint(1000,9999))
    )

    db.session.add(project)
    user.invoices_used += 1
    db.session.commit()


    flash("Project added successfully")
    return redirect("/dashboard")

@app.route("/delete_project/<int:id>")
def delete_project(id):
    project = Project.query.get(id)
    db.session.delete(project)
    db.session.commit()
    flash("Deleted!")
    return redirect("/clients")
    

@app.route("/mark_paid/<int:id>")
def mark_paid(id):
    if 'user_id' not in session:
        return redirect("/")

    user = User.query.get(session.get('user_id'))

    if not user:
        session.clear()
        return redirect("/")

    # ✅ FIRST define project
    project = Project.query.get(id)

    # ✅ THEN use it
    if not project:
        flash("Project not found ❌")
        return redirect("/clients")

    print("PROJECT USER:", project.user_id)

    # 🔒 SECURITY CHECK
    if project.user_id != user.id:
        flash("Unauthorized action ❌")
        return redirect("/clients")

    # ✅ UPDATE STATUS
    project.status = "Paid"

    db.session.commit()

    flash("Marked as Paid")
    return redirect("/clients")

def reset_monthly_usage():
    users = User.query.all()

    for user in users:
        if not user.last_reset:
            user.last_reset = datetime.utcnow()

        # 30 day cycle
        if datetime.utcnow() > user.last_reset + timedelta(days=30):
            user.invoices_used = 0
            user.emails_sent = 0
            user.last_reset = datetime.utcnow()

    db.session.commit()


@app.route("/downgrade")
def downgrade():
    if 'user_id' not in session:
        return redirect("/")

    user = User.query.get(session.get('user_id'))

    if not user:
        session.clear()
        return redirect("/")

    # downgrade to free
    user.plan = "free"
    user.trial_start = None

    # reset usage (important)
    user.invoices_used = 0
    user.emails_sent = 0

    db.session.commit()

    flash("⬇️ Downgraded to Free plan")
    return redirect("/dashboard")
# ---------------- PDF ----------------
@app.route("/download_pdf/<int:id>")
def download_pdf(id):
    user = User.query.get(session['user_id'])
    project = Project.query.get(id)
    client = Client.query.get(project.client_id)

    pdf = generate_pdf(project, client, user)
    return send_file(pdf, as_attachment=True, download_name="invoice.pdf")

# ---------------- EMAIL ----------------

def send_email_async(msg):
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=10)
        server.starttls()
        server.login(APP_EMAIL, APP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print("EMAIL ERROR:", e)




@app.route("/send_invoice/<int:id>")
def send_invoice(id):
    user = User.query.get(session['user_id'])

    if not can_use_feature(user, "email"):
        flash("🚀 Upgrade to Plus or Pro to send emails")
        return redirect("/clients")

    project = Project.query.get(id)
    client = Client.query.get(project.client_id)

    pdf = generate_pdf(project, client, user)


    msg = EmailMessage()

    msg['Subject'] = f"📄 Invoice from {user.name}"
    msg['From'] = APP_EMAIL
    msg['To'] = client.email

    # 🔥 reply goes to user
    msg['Reply-To'] = user.email

    html_content = f"""
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background-color: #f4f6f8;
                padding: 20px;
            }}
            .container {{
                max-width: 600px;
                margin: auto;
                background: white;
                border-radius: 10px;
                padding: 20px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            }}
            .header {{
                text-align: center;
                font-size: 22px;
                font-weight: bold;
                color: #1e3a8a;
                margin-bottom: 20px;
            }}
            .section {{
                margin-bottom: 15px;
            }}
            .label {{
                font-weight: bold;
            }}
            .amount {{
                font-size: 20px;
                color: green;
                font-weight: bold;
            }}
            .footer {{
                margin-top: 20px;
                font-size: 12px;
                color: #666;
                text-align: center;
            }}
        </style>
    </head>

    <body>
        <div class="container">

            <div class="header">
                📄 Invoice
            </div>

            <div class="section">
                Hello <b>{client.name}</b>,
            </div>

            <div class="section">
                <b>{user.name}</b> has sent you an invoice for the following project:
            </div>

            <div class="section">
                <span class="label">📁 Project:</span> {project.title}
            </div>

            <div class="section">
                <span class="label">💰 Amount:</span> 
                <span class="amount">{project.currency} {project.amount}</span>
            </div>

            <div class="section">
                📎 The invoice PDF is attached to this email.
            </div>

            <div class="section">
                <span class="label">📧 Contact:</span> 
                <a href="mailto:{user.email}">{user.email}</a>
            </div>

            <div class="section">
                💬 You can reply directly to this email to contact {user.name}.
            </div>

            <div class="footer">
                This invoice was sent via <b>Plexis</b> 🚀 <br>
                Secure • Professional • Trusted
            </div>

        </div>
    </body>
    </html>
    """

    # fallback text (important)
    msg.set_content(f"""
Invoice from {user.name}

Project: {project.title}
Amount: {project.currency} {project.amount}

PDF attached.

Contact: {user.email}
""")

    msg.add_alternative(html_content, subtype='html')

    # attach pdf
    msg.add_attachment(
        pdf.read(),
        maintype='application',
        subtype='pdf',
        filename="invoice.pdf"
    )

    
    threading.Thread(target=send_email_async, args=(msg,)).start()

    user.emails_sent += 1
    db.session.commit()

    


    flash("Invoice sending started 🚀")
    return redirect("/clients")

# ---------------- PLANS ----------------
@app.route("/plans")
def plans():
    user = User.query.get(session['user_id'])
    return render_template("plans.html", user=user)

@app.route("/upgrade/<plan>")
def upgrade(plan):
    if 'user_id' not in session:
        return redirect("/")

    user = User.query.get(session['user_id'])

    # 🚫 BLOCK SAME PLAN ONLY
    if user.plan == plan:
        flash(f"You are already on {plan} plan ⚠️")
        return redirect("/plans")

    # ✅ Allow both upgrade & downgrade (paid)
    return redirect(f"/create-checkout-session/{plan}")

# @app.route("/payment", methods=["GET","POST"])
# def payment():
#     user = User.query.get(session['user_id'])

#     plan = session.get("upgrade_plan", "free")
#     prices = {"plus":"$50","pro":"$100"}

#     if request.method == "POST":
#         user.plan = plan
#         user.invoices_used = 0
#         user.emails_sent = 0
#         db.session.commit()

#         flash("Plan upgraded successfully")
#         return redirect("/dashboard")

    return render_template("payment.html", plan=plan, price=prices.get(plan,"$0"))
#----------------checkout-session----------------------
@app.route("/create-checkout-session/<plan>")
def create_checkout(plan):
    user = User.query.get(session['user_id'])
    session_stripe = stripe.checkout.Session.create(
    payment_method_types=['card'],
    mode='subscription',
    customer_email=user.email,  # 🔥 ADD THIS
    metadata={
        "user_id": session['user_id'],
        "plan": plan
    },

    line_items=[{
        'price_data': {
            'currency': 'usd',
            'product_data': {'name': f"{plan} Plan"},
            'unit_amount': 1000 if plan=="plus" else 1500,
            'recurring': {'interval': 'month'}
        },
        'quantity': 1,
    }],

        success_url=f"{BASE_URL}/success",
        cancel_url=f"{BASE_URL}/cancel"
)
    return redirect(session_stripe.url)


#------------------webhook-subscription------------------#
@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET") # "whsec_33f4c906f9634da8820006eda6942b5bdc6846eaa5c116ad29a4ab554aae53d0"
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except Exception as e:
        print("❌ Webhook signature error:", e)
        return "Invalid signature", 400

    print("✅ EVENT:", event['type'])

    # 🎯 HANDLE ONLY THIS EVENT
    if event['type'] == 'checkout.session.completed':
    

        session_data = event['data']['object']

        print("🔥 SESSION:", session_data)

        metadata = session_data['metadata'] if 'metadata' in session_data else {}

        user_id = metadata['user_id'] if 'user_id' in metadata else None
        plan = metadata['plan'] if 'plan' in metadata else None

        print("USER_ID:", user_id)
        print("PLAN:", plan)

        if not user_id or not plan:
            print("❌ Missing metadata")
            return "OK", 200

        user = db.session.get(User, int(user_id))

        if user:
            user.plan = plan
            user.stripe_subscription_id = session_data['subscription']
            db.session.commit()

            print("🎉 PLAN UPDATED SUCCESSFULLY")
        else:
            print("❌ USER NOT FOUND")

    return "ok", 200

#---------success-----------#
@app.route("/success")
def success():
    return redirect("/dashboard")
    
#-------------cancel-subscription------------------#
@app.route("/cancel_subscription")
def cancel_subscription():
    if 'user_id' not in session:
        return redirect("/")

    user = db.session.get(User, session['user_id'])

    if not user or not user.stripe_subscription_id:
        flash("No active subscription ❌")
        return redirect("/dashboard")

    try:
        stripe.Subscription.delete(user.stripe_subscription_id)

        user.plan = "free"
        user.stripe_subscription_id = None
        db.session.commit()

        flash("Subscription cancelled successfully ✅")

    except Exception as e:
        print(e)
        flash("Error cancelling subscription ❌")

    return redirect("/dashboard")
#---------------overdue-date-setting------------------#
@app.route("/set_due_date/<int:id>", methods=["POST"])
def set_due_date(id):
    if 'user_id' not in session:
        return redirect("/")

    project = Project.query.get(id)

    if not project or project.user_id != session['user_id']:
        return "Unauthorized ❌"

    due_date_str = request.form.get("due_date")

    # 🔥 THIS IS IMPORTANT
    project.due_date = datetime.strptime(due_date_str, "%Y-%m-%dT%H:%M")

    project.overdue_notified = False
    project.overdue_dismissed = False

    db.session.commit()

    flash("Due date set successfully ⏰")
    return redirect("/clients")
 
@app.route("/get_overdue")
def get_overdue():
    if 'user_id' not in session:
        return {"data": []}

    user = User.query.get(session['user_id'])

    # ❌ Only PRO users
    if not user or user.plan != "pro":
        return {"data": []}

    projects = Project.query.filter(
        Project.user_id == user.id,
        Project.status == "pending",
        Project.overdue_dismissed == False
    ).all()

    data = []

    for p in projects:
      
        if p.due_date and datetime.now() > p.due_date:

        # 🔥 IMPORTANT FIX
            if not p.overdue_notified:
                continue

        client = Client.query.get(p.client_id)

        data.append({
            "id": p.id,
            "client_name": client.name if client else "Unknown",
            "client_email": client.email if client else "",
            "project_title": p.title,
            "amount": p.amount,
            "currency": p.currency,
            "email_sent": p.overdue_notified
        })
        print("PROJECT:", p.title, p.due_date, datetime.now())
    return {"data": data}

#------------overdue-checking--------------#
def check_overdue_jobs():
    with app.app_context():

        print("⏱️ Checking overdue...")

        projects = Project.query.filter_by(status="pending").all()

        for p in projects:
            
            if p.overdue_dismissed:
                continue

            if not p.due_date:
                continue
            user = db.session.get(User, p.user_id)

            # 🚫 BLOCK if not PRO
            if user.plan != "pro":
                flash("🚀 Upgrade Pro plan to use overdue!")
                continue

            # 🔥 IMPORTANT LINE
            current_time = datetime.now()

            if current_time > p.due_date:

                if p.overdue_notified:
                    continue

                print("🔥 OVERDUE:", p.title)

                user = db.session.get(User, p.user_id)
                client = db.session.get(Client, p.client_id)
            try:
                msg = EmailMessage()

                msg['Subject'] = f"⚠️ Payment Overdue - {p.title}"
                msg['From'] = APP_EMAIL
                msg['To'] = client.email
                msg['Reply-To'] = user.email


                html_content = f"""
                <html>
                <head>
                    <style>
                        body {{
                            font-family: Arial, sans-serif;
                            background-color: #f4f6f8;
                            padding: 20px;
                        }}
                        .container {{
                            max-width: 600px;
                            margin: auto;
                            background: white;
                            border-radius: 10px;
                            padding: 20px;
                            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
                        }}
                        .header {{
                            text-align: center;
                            font-size: 22px;
                            font-weight: bold;
                            color: #dc2626;
                            margin-bottom: 20px;
                        }}
                        .section {{
                            margin-bottom: 15px;
                        }}
                        .label {{
                            font-weight: bold;
                        }}
                        .amount {{
                            font-size: 20px;
                            color: #dc2626;
                            font-weight: bold;
                        }}
                        .warning {{
                            background: #fee2e2;
                            padding: 10px;
                            border-radius: 6px;
                            color: #b91c1c;
                            font-size: 14px;
                            margin-bottom: 15px;
                        }}
                        .footer {{
                            margin-top: 20px;
                            font-size: 12px;
                            color: #666;
                            text-align: center;
                        }}
                    </style>
                </head>

                <body>
                    <div class="container">

                        <div class="header">
                            ⚠️ Payment Overdue
                        </div>

                        <div class="warning">
                            This payment is past its due date. Immediate attention is required.
                        </div>

                        <div class="section">
                            Hello <b>{client.name}</b>,
                        </div>

                        <div class="section">
                            This is a reminder that payment for the following project is overdue.
                        </div>

                        <div class="section">
                            <span class="label"> Sender:</span> {user.name}
                        </div>

                        <div class="section">
                            <span class="label"> Project:</span> {p.title}
                        </div>

                        <div class="section">
                            <span class="label">Amount Due:</span> 
                            <span class="amount">{p.currency} {p.amount}</span>
                        </div>

                        <div class="section">
                            <span class="label"> Contact:</span> 
                            <a href="mailto:{user.email}">{user.email}</a>
                        </div>

                        <div class="section">
                            💬 Please reply to this email to confirm payment or discuss further.
                        </div>

                        <div class="footer">
                            This reminder was sent via <b>Plexis</b> 🚀 <br>
                            Secure • Automated • Trusted system

                        </div>

                    </div>
                </body>
                </html>
                """

                # fallback text
                msg.set_content(f"""
                ⚠️ PAYMENT OVERDUE

                Client: {client.name}
                Project: {p.title}
                Amount Due: {p.currency} {p.amount}

                Contact: {user.email}

                Please respond or complete payment as soon as possible.
                """)

                msg.add_alternative(html_content, subtype='html')
                pdf = generate_pdf(p, client, user)
                # attach PDF
                msg.add_attachment(
                            pdf.read(),
                            maintype='application',
                            subtype='pdf',
                            filename="invoice.pdf"
                        )
                    # 🔥 SEND EMAIL (THIS WAS MISSING)
                server = smtplib.SMTP('smtp.gmail.com', 587)
                server.starttls()
                server.login(APP_EMAIL, APP_PASSWORD)
                server.send_message(msg)
                server.quit()

                # ✅ mark as sent
                p.overdue_notified = True
                db.session.commit()

                print("✅ Overdue email sent")

            except Exception as e:
                print("❌ Email error:", e)


@app.route("/dismiss_overdue/<int:id>")
def dismiss_overdue(id):
    project = Project.query.get(id)

    if not project or project.user_id != session['user_id']:
        return "Unauthorized ❌"

    project.overdue_dismissed = True
    db.session.commit()

    flash("Overdue dismissed ")
    return redirect("/dashboard")


scheduler = BackgroundScheduler()

scheduler.add_job(
    func=check_overdue_jobs,
    trigger="interval",
    seconds=30,
    max_instances=1,
    coalesce=True
)

scheduler.start()

scheduler.add_job(
    func=reset_monthly_usage,
    trigger="interval",
    hours=24,   # check once per day (NOT every second)
    max_instances=1,
    coalesce=True
)

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)