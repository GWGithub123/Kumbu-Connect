"""
Database models — Users, CBOs, and cached profile data.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """User account with funder, CBO, or shared access."""
    __tablename__ = 'users'

    ROLE_FUNDER = 'funder'
    ROLE_CBO = 'cbo'
    ROLE_FUNDER_CBO = 'funder_cbo'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    password_is_temporary = db.Column(db.Boolean, nullable=False, default=False)
    role = db.Column(db.String(20), nullable=False)          # 'funder' | 'cbo' | 'funder_cbo'
    account_status = db.Column(db.String(20), nullable=False, default='active')
    display_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # If role == 'cbo', link to exactly one CBO profile
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=True)
    cbo = db.relationship('CBO', backref='users', foreign_keys=[cbo_id])

    def set_password(self, password, temporary: bool = False):
        self.password_hash = generate_password_hash(password)
        self.password_is_temporary = bool(temporary)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def needs_password_setup(self) -> bool:
        return bool(self.has_role(self.ROLE_CBO) and self.cbo_id is not None and self.password_is_temporary)

    def has_role(self, role: str) -> bool:
        normalized = str(role or '').strip().lower()
        if normalized == self.ROLE_FUNDER:
            return self.role in {self.ROLE_FUNDER, self.ROLE_FUNDER_CBO}
        if normalized == self.ROLE_CBO:
            return self.role in {self.ROLE_CBO, self.ROLE_FUNDER_CBO}
        return self.role == normalized

    @property
    def is_funder(self) -> bool:
        return self.has_role(self.ROLE_FUNDER)

    @property
    def is_cbo(self) -> bool:
        return self.has_role(self.ROLE_CBO) and self.cbo_id is not None

    @property
    def role_badge_class(self) -> str:
        return self.ROLE_FUNDER if self.role == self.ROLE_FUNDER_CBO else self.role

    @property
    def role_badge_label(self) -> str:
        if self.role == self.ROLE_FUNDER_CBO:
            return 'FUNDER + CBO'
        return str(self.role or '').upper()


class CBO(db.Model):
    """A Community-Based Organisation profile."""
    __tablename__ = 'cbos'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False)

    # KoboToolbox link
    kobo_asset_id = db.Column(db.String(100), nullable=True)
    kobo_connection_active = db.Column(db.Boolean, nullable=False, default=True)
    kobo_disconnected_at = db.Column(db.DateTime, nullable=True)
    cbo_identifier = db.Column(db.String(50), nullable=True)  # Identifier to filter KoboToolbox data
    sms_keyword = db.Column(db.String(50), unique=True, nullable=True)
    community_prompt = db.Column(db.Text, default='')
    community_feedback_enabled = db.Column(db.Boolean, default=True)

    # ── Identity & metadata ──
    location = db.Column(db.String(255), default='')
    street_address = db.Column(db.String(255), default='')
    formatted_address = db.Column(db.String(255), default='')
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    geocode_query = db.Column(db.String(255), default='')
    geocoded_at = db.Column(db.DateTime, nullable=True)
    place_id = db.Column(db.String(255), default='')
    county_region = db.Column(db.String(255), default='')
    tool_inventory_total = db.Column(db.Integer, nullable=True)
    org_type = db.Column(db.String(255), default='Community-Based Organisation (CBO)')
    founded_year = db.Column(db.String(10), default='')
    focus_areas = db.Column(db.String(500), default='Rural livelihood, subsistence agriculture, youth')

    # ── Leadership ──
    chairperson = db.Column(db.String(255), default='')
    program_director = db.Column(db.String(255), default='')
    finance_lead = db.Column(db.String(255), default='')

    # ── Quantified Social Impact (JSON blob) ──
    impact_json = db.Column(db.Text, default='{}')

    # ── Flagship project summary ──
    flagship_summary = db.Column(db.Text, default='')

    # ── Community success story ──
    success_story = db.Column(db.Text, default='')

    # ── Join Us / CTA ──
    join_us_text = db.Column(db.Text, default='')

    # ── Customized bookkeeping workspace ──
    bookkeeping_template_json = db.Column(db.Text, default='{}')
    bookkeeping_workspace_entries_json = db.Column(db.Text, default='[]')

    # ── Google Forms intake ──
    intake_form_id = db.Column(db.String(255), default='')
    intake_form_edit_url = db.Column(db.String(500), default='')
    intake_form_responder_url = db.Column(db.String(500), default='')

    # ── Raw Kobo data cache ──
    raw_kobo_json = db.Column(db.Text, default='[]')

    # ── Full AI-generated profile (JSON) ──
    ai_profile_json = db.Column(db.Text, default='{}')

    # ── Growth Metrics (time-series JSON) ──
    growth_metrics_json = db.Column(db.Text, default='[]')
    # Format: [{"month": "2024-06", "rentals": 15, "borrowers": 8, "revenue": 350, ...}, ...]

    # ── Classification, badges, scores ──
    classifications_json = db.Column(db.Text, default='[]')   # e.g. ["education","healthcare"]
    data_quality_badge   = db.Column(db.String(10), default='')  # "bronze"|"silver"|"gold"
    social_impact_score  = db.Column(db.Integer, default=0)      # 0–100

    # Timestamps
    last_synced = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def has_kobo_connection(self) -> bool:
        return bool(self.kobo_connection_active and self.kobo_asset_id)

    def disconnect_kobo(self):
        self.kobo_connection_active = False
        self.kobo_disconnected_at = datetime.utcnow()
        self.kobo_asset_id = None
        self.raw_kobo_json = '[]'


class SavedCBO(db.Model):
    """A CBO saved by a funder for later review."""
    __tablename__ = 'saved_cbos'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'cbo_id', name='uq_saved_cbo_user_cbo'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship('User', backref=db.backref('saved_cbo_entries', lazy=True, cascade='all, delete-orphan'))
    cbo = db.relationship('CBO', backref=db.backref('saved_by_entries', lazy=True, cascade='all, delete-orphan'))


class CBOContactThread(db.Model):
    """A funder-to-CBO conversation thread."""
    __tablename__ = 'cbo_contact_threads'
    __table_args__ = (
        db.UniqueConstraint('cbo_id', 'funder_user_id', name='uq_cbo_contact_thread_cbo_funder'),
    )

    id = db.Column(db.Integer, primary_key=True)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    funder_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cbo = db.relationship(
        'CBO',
        backref=db.backref('contact_threads', lazy=True, order_by='desc(CBOContactThread.last_message_at)'),
    )
    funder = db.relationship(
        'User',
        backref=db.backref('cbo_contact_threads', lazy=True, order_by='desc(CBOContactThread.last_message_at)'),
        foreign_keys=[funder_user_id],
    )


class CBOContactMessage(db.Model):
    """A single message or file shared inside a funder-to-CBO thread."""
    __tablename__ = 'cbo_contact_messages'

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('cbo_contact_threads.id'), nullable=False)
    sender_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    sender_role = db.Column(db.String(20), nullable=False, default='funder')
    message_body = db.Column(db.Text, default='')
    original_filename = db.Column(db.String(255), default='')
    mime_type = db.Column(db.String(100), default='application/octet-stream')
    storage_backend = db.Column(db.String(20), nullable=False, default='local')
    storage_bucket = db.Column(db.String(255), default='')
    storage_object_path = db.Column(db.String(500), default='')
    stored_path = db.Column(db.String(500), default='')
    file_size_bytes = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    read_at = db.Column(db.DateTime, nullable=True)

    thread = db.relationship(
        'CBOContactThread',
        backref=db.backref('messages', lazy=True, order_by='CBOContactMessage.created_at'),
        foreign_keys=[thread_id],
    )
    sender = db.relationship(
        'User',
        backref=db.backref('cbo_contact_messages', lazy=True, order_by='desc(CBOContactMessage.created_at)'),
        foreign_keys=[sender_user_id],
    )


class BookkeepingDocument(db.Model):
    """An uploaded bookkeeping image plus extracted structured accounting data."""
    __tablename__ = 'bookkeeping_documents'

    id = db.Column(db.Integer, primary_key=True)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    upload_batch_id = db.Column(db.String(255), default='')
    client_submission_id = db.Column(db.String(255), default='')
    original_filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(500), nullable=False)
    storage_backend = db.Column(db.String(20), nullable=False, default='local')
    storage_bucket = db.Column(db.String(255), default='')
    storage_object_path = db.Column(db.String(500), default='')
    mime_type = db.Column(db.String(100), nullable=False, default='image/jpeg')
    source_channel = db.Column(db.String(50), nullable=False, default='web_upload')
    include_in_workspace = db.Column(db.Boolean, nullable=False, default=False)
    workspace_period_key = db.Column(db.String(7), default='')
    document_type = db.Column(db.String(50), nullable=False, default='unknown')
    document_date = db.Column(db.String(20), default='')
    period_start = db.Column(db.String(20), default='')
    period_end = db.Column(db.String(20), default='')
    vendor_or_counterparty = db.Column(db.String(255), default='')
    currency = db.Column(db.String(10), default='KES')
    summary_text = db.Column(db.Text, default='')
    extraction_confidence = db.Column(db.Float, default=0.0)
    total_income = db.Column(db.Float, default=0.0)
    total_expenses = db.Column(db.Float, default=0.0)
    net_amount = db.Column(db.Float, default=0.0)
    extracted_data_json = db.Column(db.Text, default='{}')
    firestore_synced_at = db.Column(db.DateTime, nullable=True)
    processed_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cbo = db.relationship('CBO', backref=db.backref('bookkeeping_documents', lazy=True, order_by='desc(BookkeepingDocument.created_at)'))
    uploaded_by = db.relationship('User', backref=db.backref('bookkeeping_uploads', lazy=True), foreign_keys=[uploaded_by_user_id])


class FundingAuditDocument(db.Model):
    """An uploaded grant or donation document plus verification metadata."""
    __tablename__ = 'funding_audit_documents'

    id = db.Column(db.Integer, primary_key=True)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(500), nullable=False, default='')
    storage_backend = db.Column(db.String(20), nullable=False, default='local')
    mime_type = db.Column(db.String(100), nullable=False, default='application/pdf')
    source_channel = db.Column(db.String(50), nullable=False, default='web_upload')
    document_type = db.Column(db.String(50), nullable=False, default='unknown')
    document_date = db.Column(db.String(20), default='')
    declared_funder_name = db.Column(db.String(255), default='')
    extracted_funder_name = db.Column(db.String(255), default='')
    extracted_reference_number = db.Column(db.String(255), default='')
    declared_period_start = db.Column(db.String(20), default='')
    declared_period_end = db.Column(db.String(20), default='')
    currency = db.Column(db.String(10), default='KES')
    declared_funding_amount = db.Column(db.Float, default=0.0)
    declared_working_capital = db.Column(db.Float, default=0.0)
    summary_text = db.Column(db.Text, default='')
    verification_status = db.Column(db.String(20), default='needs_review')
    verification_confidence = db.Column(db.Float, default=0.0)
    extracted_data_json = db.Column(db.Text, default='{}')
    processed_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cbo = db.relationship('CBO', backref=db.backref('funding_audit_documents', lazy=True, order_by='desc(FundingAuditDocument.created_at)'))
    uploaded_by = db.relationship('User', backref=db.backref('funding_audit_uploads', lazy=True), foreign_keys=[uploaded_by_user_id])


class GoogleFormResponse(db.Model):
    """A cached Google Form submission for a CBO intake form."""
    __tablename__ = 'google_form_responses'
    __table_args__ = (
        db.UniqueConstraint('form_id', 'response_id', name='uq_google_form_form_response'),
    )

    id = db.Column(db.Integer, primary_key=True)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    form_id = db.Column(db.String(255), nullable=False, default='')
    response_id = db.Column(db.String(255), nullable=False, default='')
    respondent_email = db.Column(db.String(255), default='')
    response_created_at = db.Column(db.DateTime, nullable=True)
    response_submitted_at = db.Column(db.DateTime, nullable=True)
    answers_json = db.Column(db.Text, default='[]')
    raw_response_json = db.Column(db.Text, default='{}')
    sync_status = db.Column(db.String(20), default='synced')
    sync_error = db.Column(db.Text, default='')
    provisioning_status = db.Column(db.String(20), default='pending')
    provisioning_error = db.Column(db.Text, default='')
    provisioned_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    provisioned_cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=True)
    provisioned_at = db.Column(db.DateTime, nullable=True)
    synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cbo = db.relationship('CBO', backref=db.backref('google_form_responses', lazy=True, order_by='desc(GoogleFormResponse.response_submitted_at)'), foreign_keys=[cbo_id])
    provisioned_user = db.relationship('User', backref=db.backref('provisioned_google_form_responses', lazy=True), foreign_keys=[provisioned_user_id])
    provisioned_cbo = db.relationship('CBO', backref=db.backref('provisioned_google_form_responses', lazy=True), foreign_keys=[provisioned_cbo_id])


class GoogleFormUpload(db.Model):
    """A file uploaded through a Google Form response."""
    __tablename__ = 'google_form_uploads'
    __table_args__ = (
        db.UniqueConstraint('drive_file_id', name='uq_google_form_upload_drive_file'),
    )

    id = db.Column(db.Integer, primary_key=True)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    google_form_response_id = db.Column(db.Integer, db.ForeignKey('google_form_responses.id'), nullable=False)
    bookkeeping_document_id = db.Column(db.Integer, db.ForeignKey('bookkeeping_documents.id'), nullable=True)
    drive_file_id = db.Column(db.String(255), nullable=False, default='')
    drive_file_url = db.Column(db.String(500), default='')
    question_id = db.Column(db.String(255), default='')
    question_title = db.Column(db.String(255), default='')
    upload_kind = db.Column(db.String(50), default='other')
    original_filename = db.Column(db.String(255), default='')
    mime_type = db.Column(db.String(100), default='application/octet-stream')
    storage_backend = db.Column(db.String(20), default='local')
    stored_path = db.Column(db.String(500), default='')
    sync_status = db.Column(db.String(20), default='pending')
    processing_error = db.Column(db.Text, default='')
    processed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cbo = db.relationship('CBO', backref=db.backref('google_form_uploads', lazy=True, order_by='desc(GoogleFormUpload.created_at)'))
    response = db.relationship('GoogleFormResponse', backref=db.backref('uploads', lazy=True, order_by='desc(GoogleFormUpload.created_at)'))
    bookkeeping_document = db.relationship('BookkeepingDocument', backref=db.backref('google_form_uploads', lazy=True), foreign_keys=[bookkeeping_document_id])


class CommunitySubscriber(db.Model):
    """A community member who opts into SMS feedback for a CBO."""
    __tablename__ = 'community_subscribers'
    __table_args__ = (
        db.UniqueConstraint('cbo_id', 'phone_number', name='uq_community_subscriber_cbo_phone'),
    )

    id = db.Column(db.Integer, primary_key=True)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    phone_number = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='active')
    signup_source = db.Column(db.String(20), nullable=False, default='sms')
    signup_keyword = db.Column(db.String(50), default='')
    consent_received_at = db.Column(db.DateTime, nullable=True)
    last_response_at = db.Column(db.DateTime, nullable=True)
    last_checkin_sent_at = db.Column(db.DateTime, nullable=True)
    conversation_state = db.Column(db.String(32), nullable=False, default='idle')
    active_feedback_id = db.Column(db.Integer, db.ForeignKey('community_feedback.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cbo = db.relationship('CBO', backref=db.backref('community_subscribers', lazy=True))
    active_feedback = db.relationship('CommunityFeedback', foreign_keys=[active_feedback_id], post_update=True)


class CommunityFeedback(db.Model):
    """A structured SMS feedback response captured from a community member."""
    __tablename__ = 'community_feedback'

    id = db.Column(db.Integer, primary_key=True)
    subscriber_id = db.Column(db.Integer, db.ForeignKey('community_subscribers.id'), nullable=False)
    cbo_id = db.Column(db.Integer, db.ForeignKey('cbos.id'), nullable=False)
    cycle_type = db.Column(db.String(20), nullable=False, default='onboarding')
    delivery_channel = db.Column(db.String(20), nullable=False, default='sms')
    questionnaire_version = db.Column(db.String(20), nullable=False, default='v1')
    status = db.Column(db.String(20), nullable=False, default='in_progress')
    rating = db.Column(db.Integer, nullable=True)
    help_count = db.Column(db.Integer, nullable=True)
    anecdote = db.Column(db.Text, default='')
    raw_transcript = db.Column(db.Text, default='[]')
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    follow_up_due_at = db.Column(db.DateTime, nullable=True)
    firestore_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subscriber = db.relationship(
        'CommunitySubscriber',
        foreign_keys=[subscriber_id],
        backref=db.backref('feedback_entries', lazy=True),
    )
    cbo = db.relationship('CBO', backref=db.backref('community_feedback_entries', lazy=True))
