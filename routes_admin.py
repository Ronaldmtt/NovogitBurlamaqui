# routes_admin.py
from functools import wraps
import logging
import secrets
import string

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from extensions import db
from models import User

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Acesso negado. Área restrita a administradores.", "danger")
            return redirect(url_for('core.dashboard'))
        return f(*args, **kwargs)
    return decorated_function


@admin_bp.route("/users")
@login_required
@admin_required
def users_list():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin/users.html", users=users)


@admin_bp.route("/users/new", methods=["GET", "POST"])
@login_required
@admin_required
def user_create():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        is_admin = request.form.get("is_admin") == "on"
        is_active = request.form.get("is_active") == "on"
        max_workers = request.form.get("max_workers", 5, type=int)
        elaw_username = request.form.get("elaw_username", "").strip() or None
        elaw_password = request.form.get("elaw_password", "").strip() or None

        if not username or not email or not password:
            flash("Username, email e senha são obrigatórios.", "danger")
            return render_template("admin/user_form.html", user=None, is_edit=False)

        if len(password) < 6:
            flash("A senha deve ter no mínimo 6 caracteres.", "danger")
            return render_template("admin/user_form.html", user=None, is_edit=False)

        if User.query.filter_by(username=username).first():
            flash("Username já existe.", "danger")
            return render_template("admin/user_form.html", user=None, is_edit=False)

        if User.query.filter_by(email=email).first():
            flash("Email já existe.", "danger")
            return render_template("admin/user_form.html", user=None, is_edit=False)

        try:
            user = User(
                username=username,
                email=email,
                is_admin=is_admin,
                is_active=is_active,
                max_workers=max_workers,
                elaw_username=elaw_username,
                elaw_password=elaw_password,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Usuário '{username}' criado com sucesso!", "success")
            logger.info(f"[ADMIN] Usuário '{username}' criado por {current_user.username}")
            return redirect(url_for("admin.users_list"))
        except Exception as e:
            db.session.rollback()
            logger.error(f"[ADMIN] Erro ao criar usuário: {e}")
            flash(f"Erro ao criar usuário: {str(e)}", "danger")
            return render_template("admin/user_form.html", user=None, is_edit=False)

    return render_template("admin/user_form.html", user=None, is_edit=False)


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def user_edit(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        is_admin = request.form.get("is_admin") == "on"
        is_active = request.form.get("is_active") == "on"
        max_workers = request.form.get("max_workers", 5, type=int)
        elaw_username = request.form.get("elaw_username", "").strip() or None
        elaw_password = request.form.get("elaw_password", "").strip() or None

        if not username or not email:
            flash("Username e email são obrigatórios.", "danger")
            return render_template("admin/user_form.html", user=user, is_edit=True)

        existing_user = User.query.filter_by(username=username).first()
        if existing_user and existing_user.id != user.id:
            flash("Username já existe.", "danger")
            return render_template("admin/user_form.html", user=user, is_edit=True)

        existing_email = User.query.filter_by(email=email).first()
        if existing_email and existing_email.id != user.id:
            flash("Email já existe.", "danger")
            return render_template("admin/user_form.html", user=user, is_edit=True)

        if user.id == current_user.id and not is_admin:
            flash("Você não pode remover seu próprio status de administrador.", "warning")
            is_admin = True

        if user.id == current_user.id and not is_active:
            flash("Você não pode desativar sua própria conta.", "warning")
            is_active = True

        try:
            user.username = username
            user.email = email
            user.is_admin = is_admin
            user.is_active = is_active
            user.max_workers = max_workers
            user.elaw_username = elaw_username
            user.elaw_password = elaw_password
            db.session.commit()
            flash(f"Usuário '{username}' atualizado com sucesso!", "success")
            logger.info(f"[ADMIN] Usuário '{username}' editado por {current_user.username}")
            return redirect(url_for("admin.users_list"))
        except Exception as e:
            db.session.rollback()
            logger.error(f"[ADMIN] Erro ao editar usuário: {e}")
            flash(f"Erro ao editar usuário: {str(e)}", "danger")
            return render_template("admin/user_form.html", user=user, is_edit=True)

    return render_template("admin/user_form.html", user=user, is_edit=True)


@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def user_toggle(user_id):
    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("Você não pode desativar sua própria conta.", "warning")
        return redirect(url_for("admin.users_list"))

    try:
        user.is_active = not user.is_active
        db.session.commit()
        status = "ativado" if user.is_active else "desativado"
        flash(f"Usuário '{user.username}' {status} com sucesso!", "success")
        logger.info(f"[ADMIN] Usuário '{user.username}' {status} por {current_user.username}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"[ADMIN] Erro ao alterar status do usuário: {e}")
        flash(f"Erro ao alterar status do usuário: {str(e)}", "danger")

    return redirect(url_for("admin.users_list"))


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def user_reset_password(user_id):
    user = User.query.get_or_404(user_id)

    new_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))

    try:
        user.set_password(new_password)
        db.session.commit()
        flash(f"Senha do usuário '{user.username}' resetada para: {new_password}", "success")
        logger.info(f"[ADMIN] Senha do usuário '{user.username}' resetada por {current_user.username}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"[ADMIN] Erro ao resetar senha: {e}")
        flash(f"Erro ao resetar senha: {str(e)}", "danger")

    return redirect(url_for("admin.users_list"))
