#!/usr/bin/env python2.7
from __future__ import absolute_import, print_function
from base64 import b64decode, b64encode
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Attr as AttrCondition
from boto3.session import Session as Boto3Session
from cracklib import VeryFascistCheck
from cStringIO import StringIO
from datetime import datetime, timedelta
from dateutil.tz import tzutc
from distutils.util import strtobool
from flask import (
    escape, flash, Flask, g, make_response, redirect, render_template, request,
    session, url_for,
)
from functools import wraps
from httplib import (
    BAD_REQUEST, FORBIDDEN, OK, SERVICE_UNAVAILABLE, UNAUTHORIZED)
from os import close, environ, fsync, urandom, write
from passlib.hash import pbkdf2_sha512
from random import randint
import requests
from shutil import rmtree
from six import text_type
from string import ascii_letters, digits
from subprocess import Popen, PIPE
from sys import exit, stderr, stdin, stdout
from tempfile import mkdtemp, mkstemp
from time import time
from validate_email import validate_email
from zappa.handler import lambda_handler

if "ENCRYPTION_KEY_ID" not in environ:
    print("FATAL: ENCRYPTION_KEY_ID environment variable must be set.",
          file=stderr)
    exit(1)

# Number of API retries
n_retries = 5

# You shouldn't need to customize anything below this line.
b3 = Boto3Session()

# AWS service handles
apigw = b3.client("apigateway")
ddb = b3.resource("dynamodb")
ddb_table_prefix = environ.get("LABCAFE_TABLE_PREFIX", "LabCafe")
ddb_events = ddb.Table(ddb_table_prefix + ".Events")
ddb_users = ddb.Table(ddb_table_prefix + ".Users")
ec2 = b3.client("ec2")
kms = b3.client("kms")

# This is a hash for a password that can't be deduced. It was generated by:
# pbkdf2_sha512.encrypt(urandom(128), rounds=96000)
invalid_password_hash = (
    "$pbkdf2-sha512"
    "$96000"
    "$8Z6TkrI2ZsyZUwoBYOy99w"
    "$8OJdNMyRfmUcLFTvK5bxxAy4Bal.X1r1J75VsW/DD4"
    "OmSXpbvYOERa4RBWSR0D2lch7sEU2wFtKfEl5IlUaQSQ")

# The attributes on a user to return from DynamoDB (excludes HashedPassword)
user_attributes = ",".join(
    ["Email", "EventId", "InstanceId", "FullName", "AllowContact",
     "CreationDate", "SSHPrivateKey", "SSHPublicKey", "UserId"]
)

app = Flask(__name__)

# Configure tuneables for Flask
app.config["DEBUG"] = strtobool(
    environ.get("DEBUG", "False"))
app.config["TEMPLATES_AUTO_RELOAD"] = strtobool(
    environ.get("TEMPLATES_AUTO_RELOAD", "False"))
app.config["ENCRYPTION_KEY_ID"] = environ["ENCRYPTION_KEY_ID"]
app.config["ENCRYPTION_CONTEXT"] = {
    "Application": "AWSLabCafe",
    "LambdaFunctionName": environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
}
app.config["PBKDF2_SHA512_ROUNDS"] = 96000


def set_secret_key(app):
    """
    set_secret_key(app)

    Set the SECRET_KEY field of the Flask app config using the value stored in
    DynamoDB.
    """
    # Attempt to fetch this from DyanmoDB first. This will succeed every time
    # after the first invocation.
    try:
        result = ddb_events.get_item(Key={"EventId": "_"}, ConsistentRead=True)
    except ClientError as e:
        import traceback
        traceback.print_exc()
        result = {}

    item = result.get("Item", {})
    secret_key_encrypted = item.get("SecretKey")
    if secret_key_encrypted:
        result = kms.decrypt(
            CiphertextBlob=b64decode(secret_key_encrypted),
            EncryptionContext=app.config["ENCRYPTION_CONTEXT"]
        )

        secret_key = result.get("Plaintext")
        if secret_key:
            app.config["SECRET_KEY"] = secret_key
            return

    # This doesn't exist yet. Generate a new key.
    secret_key = urandom(16)

    print(encryption_context)
    # Encrypt it with our KMS key.
    result = kms.encrypt(
        KeyId=app.config["ENCRYPTION_KEY_ID"],
        Plaintext=secret_key,
        EncryptionContext=app.config["ENCRYPTION_CONTEXT"]
    )

    secret_key_encrypted = result["CiphertextBlob"]

    # Write this out *only if* nobody else has updated it in the meantime.
    result = ddb_events.update_item(
        Key={"EventId": "_"},
        UpdateExpression=(
            "SET SecretKey = if_not_exists(SecretKey, :new_secret_key)"),
        ExpressionAttributeValues={
            ":new_secret_key": b64encode(secret_key_encrypted)
        },
        ReturnValues="ALL_NEW")

    # Always retrieve the value from DyanmoDB; this might not be our generated
    # key (if there's a concurrent update).
    secret_key_encrypted = b64decode(result["Attributes"]["SecretKey"])

    result = kms.decrypt(
        CiphertextBlob=secret_key_encrypted,
        EncryptionContext=app.config["ENCRYPTION_CONTEXT"]
    )

    app.config["SECRET_KEY"] = result.get("Plaintext")
    return

set_secret_key(app)

# Items required by the Jijna templates
app.jinja_env.globals["static_prefix"] = "static/"
app.jinja_env.globals["prefix"] = "/"
app.jinja_env.globals["datetime"] = datetime
app.jinja_env.globals["timedelta"] = timedelta
app.jinja_env.globals["tzutc"] = tzutc


def is_valid_event_id(event_id):
    """
    Indicates whether this is a valid event id.
    """
    response = ddb_events.get_item(
        Key={"EventId": event_id},
        ProjectionExpression="EventName",
        ReturnConsumedCapacity="TOTAL",
    )

    item = response.get("Item")
    return item is not None
app.jinja_env.globals["is_valid_event_id"] = is_valid_event_id


def get_instance_info(instance_id):
    """
    Return the public IP address for a given instance id.
    """
    response = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = response.get("Reservations")
    if not reservations:
        return None

    instances = response["Reservations"][0]["Instances"]
    if not instances:
        return None

    return instances[0]
app.jinja_env.globals["get_instance_info"] = get_instance_info


def get_user(email, event_id):
    """
    If this is a valid user email and event id pair (and the event id is still
    valid), return details about the user. Otherwise, returns None.
    """
    response = ddb_users.get_item(
        Key={"Email": email, "EventId": event_id},
        ProjectionExpression=user_attributes,
        ReturnConsumedCapacity="TOTAL",
    )

    item = response.get("Item")
    if item is not None and is_valid_event_id(event_id):
        return item

    return None


def login_user(email, password, event_id):
    """
    If this user is known and passes authentication checks, returns details
    about the user and sets session details. Otherwise, returns None.
    """
    response = ddb_users.get_item(
        Key={"Email": email, "EventId": event_id},
        ProjectionExpression=(user_attributes + ",PasswordHash"),
        ReturnConsumedCapacity="TOTAL",
    )

    # We ALWAYS perform a password verification to prevent timing-based
    # attacks.  If we skip this when a user is not found, an attacker can
    # deduce whether an email has been registered by monitoring the time it
    # takes for verification.
    item = response.get("Item")
    if item is None:
        password_hash = invalid_password_hash
    else:
        password_hash = item.pop("PasswordHash", "")

    if not pbkdf2_sha512.verify(password, password_hash):
        return None

    session["Email"] = email
    session["EventId"] = event_id

    return item


def generate_private_public_key(comment="", bits=2048):
    """
    generate_private_public_key(comment="", bits=2048) -> dict
    Generate an OpenSSH private/public keypair.

    The resulting dict has the form:
        { "PrivateKey": private_key, "PublicKey": public_key }
    """
    if bits not in (1024, 2048, 4096):
        raise ValueError("bits must be 1024, 2048, or 4096")

    tempdir = mkdtemp()
    proc = Popen(["/usr/bin/ssh-keygen", "-f", "%s/key" % tempdir, "-t", "rsa",
                  "-b", str(bits), "-P", "", "-C", comment])
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("Failed to generate private key: %s" % err.strip())

    with open("%s/key" % tempdir, "rb") as privkey_fp:
        private_key = privkey_fp.read()

    with open("%s/key.pub" % tempdir, "rb") as pubkey_fp:
        public_key = pubkey_fp.read()

    rmtree(tempdir, ignore_errors=True)

    return {"PrivateKey": private_key, "PublicKey": public_key}


def register_user(email, password, event_id, full_name, allow_contact):
    """
    If the user does not already exist for this event, register him/her.
    """
    pwhash = pbkdf2_sha512.encrypt(
        password, rounds=app.config["PBKDF2_SHA512_ROUNDS"])
    keys = generate_private_public_key()

    user_item = {
        "Email": email,
        "EventId": event_id,
        "PasswordHash": pwhash,
        "FullName": full_name,
        "AllowContact": allow_contact,
        "CreationDate": int(time()),
        "SSHPrivateKey": keys["PrivateKey"],
        "SSHPublicKey": keys["PublicKey"],
    }

    # Get the next user id.
    while True:
        event_item = ddb_events.get_item(
            Key={"EventId": event_id},
            ProjectionExpression="NextUID",
            ReturnConsumedCapacity="TOTAL",
        )["Item"]

        user_id = event_item["NextUID"]

        try:
            ddb_events.update_item(
                Key={"EventId": event_id},
                UpdateExpression="SET NextUID = NextUID + :incr",
                ConditionExpression="NextUID = :current_uid",
                ExpressionAttributeValues={
                    ":current_uid": user_id,
                    ":incr": 1,
                },
                ReturnConsumedCapacity="TOTAL",
            )
            break
        except ClientError as e:
            error_code = (
                getattr(e, "response", {}).get("Error", {}).get("Code", ""))
            if error_code != u"ConditionalCheckFailedException":
                raise

            # Concurrent modification; try again.

    user_item["UserId"] = user_id

    try:
        ddb_users.put_item(
            Item=user_item,
            ConditionExpression="attribute_not_exists(EventId)",
            ReturnConsumedCapacity="TOTAL",
        )
    except ClientError as e:
        response = getattr(e, "response", {})
        error = response.get("Error", {})
        error_code = error.get("Code", "")

        if error_code == u"ConditionalCheckFailedException":
            # User already exists.
            return None
        raise

    session["Email"] = email
    session["EventId"] = event_id
    del user_item["PasswordHash"]
    return user_item


# CSRF protection
@app.before_request
def csrf_protect():
    if request.method == "POST":
        cookie_token = session.get("_csrf_token")
        form_token = request.form.get("_csrf_token")
        if not cookie_token or cookie_token != form_token:
            print("CSRF token mismatch:\n    Cookie: %s\n    Form: %s" %
                  (cookie_token, form_token))
            return make_response(render_template("error.html"), FORBIDDEN)
    return


def generate_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = b64encode(urandom(36))
    return session["_csrf_token"]
app.jinja_env.globals["csrf_token"] = generate_csrf_token


def require_valid_session(f):
    @wraps(f)
    def wrapper(*args, **kw):
        email = session.get("Email", None)
        event_id = session.get("EventId", None)

        if email is None or event_id is None:
            return redirect("/login")

        request.user = get_user(email, event_id)
        if request.user is None:
            del session["Email"]
            del session["EventId"]
            return redirect("/login")

        return f(*args, **kw)

    return wrapper


@app.route("/", methods=["GET"])
@require_valid_session
def index(**kw):
    instance_id = request.user.get("InstanceId")
    if instance_id:
        instance_info = get_instance_info(instance_id)

        if (instance_info is None or (
                instance_info["State"]["Name"] == u"terminated")):
            ec2_clear_user_instance()
            instance_id = None
    else:
        instance_info = None

    return render_template(
        "index.html", user=request.user, instance_id=instance_id,
        instance_info=instance_info)


@app.route("/screenshot", methods=["GET"])
@require_valid_session
def screenshot(**kw):
    return render_template("screenshot.html", user=request.user)


@app.route("/ec2/screenshot", methods=["GET"])
@require_valid_session
def ec2_screenshot(**kw):
    instance_id = request.user.get("InstanceId")
    if not instance_id:
        return make_response(("You do not have an EC2 instance assigned.",
                              BAD_REQUEST, {}))

    response = ec2.get_console_screenshot(InstanceId=instance_id, WakeUp=True)
    image_data = response.get("ImageData")
    if not image_data:
        return make_response(
            ("No console screenshot returned.", SERVICE_UNAVAILABLE,
             {"Content-Type": "text/plain"}))

    image_data = b64decode(image_data)
    return make_response((
        image_data, OK,
        {
            "Cache-Control": "max-age=30",
            "Content-Type": "image/jpeg"
        }))


@app.route("/ec2", methods=["POST"])
@require_valid_session
def ec2_post(**kw):
    action = request.form.get("Action")

    if action == "Launch":
        return ec2_launch()
    elif action == "Terminate":
        return ec2_terminate()
    elif action == "Start":
        return ec2_start()
    elif action == "Stop":
        return ec2_stop()
    elif action == "Reboot":
        return ec2_reboot()

    flash("<b>Invalid EC2 action: %s</b>" % action, category="error")
    return redirect("/")


def ec2_clear_user_instance():
    ddb_users.update_item(
        Key={
            "Email": request.user["Email"],
            "EventId": request.user["EventId"],
        },
        UpdateExpression="REMOVE InstanceId",
        ReturnConsumedCapacity="TOTAL",
    )
    return


def ec2_launch():
    # Make sure the user doesn't already have an EC2 instance.
    if request.user.get("InstanceId"):
        flash("You already have an EC2 instance assigned.", category="info")
        return redirect("/")

    # Get the instance specs.
    # TODO: Allow AMI parameter and check AllowedAMIs in HPCLab.Events
    # TODO: Allow InstanceType parameter and check AllowedInstanceTypes
    # TODO: Allow SecurityGroup parameters and check AllowedSecurityGroups
    item = ddb_events.get_item(
        Key={"EventId": request.user["EventId"]},
        ProjectionExpression=(
            "AdminSSHKey,AllowedSubnets,DefaultAMI,DefaultInstanceType,"
            "DefaultSecurityGroup,DefaultVolumeSize,EFSId"
        ),
        ReturnConsumedCapacity="TOTAL",
    )["Item"]

    admin_ssh_key = item.get("AdminSSHKey")
    subnets = list(item["AllowedSubnets"])
    ami = item["DefaultAMI"]
    instance_type = item["DefaultInstanceType"]
    security_group = item["DefaultSecurityGroup"]
    volume_size = item["DefaultVolumeSize"]
    efs_id = item["EFSId"]

    # Choose a subnet and get its availability zone.
    subnet = subnets[randint(0, len(subnets) - 1)]
    sn_info = ec2.describe_subnets(SubnetIds=[subnet])["Subnets"][0]
    az = sn_info["AvailabilityZone"]
    region = az[:-1]

    # Sanitize the user's full name
    fullname_sanitized = "".join([
        c for c in request.user["FullName"]
        if c in (ascii_letters + digits + " ',./!@#%^&*()-_=+")])

    # Launch userdata for mounting the EFS volume, creating the user, and
    # creating the home directory (if needed).
    user_info = """\
#!/bin/bash
yum install -y nfs-utils
setsebool -P use_nfs_home_dirs 1 || true
mkdir /efshome
mount -t nfs4 \
-o nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2 \
%(az)s.%(efs_id)s.efs.%(region)s.amazonaws.com:/ /efshome
echo %(az)s.%(efs_id)s.efs.%(region)s.amazonaws.com:/ /efshome nfs \
nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2 0 0 >> \
/etc/fstab
mkdir -p /efshome/lab%(user_id)d/.ssh
ln -s /efshome/lab%(user_id)d /home/lab%(user_id)d
groupadd --gid %(user_id)d lab%(user_id)d
useradd --base-dir /home --comment "%(fullname_sanitized)s" \
--create-home --gid %(user_id)d --uid %(user_id)d lab%(user_id)d
echo 'lab%(user_id)d ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
cat >> /home/lab%(user_id)d/.ssh/authorized_keys << .EOF
%(public_key)s
.EOF
chmod 0755 /efshome/lab%(user_id)d/.ssh
chmod 0644 /efshome/lab%(user_id)d/.ssh/authorized_keys
chown -R lab%(user_id)d:lab%(user_id)d /efshome/lab%(user_id)d
    """ % {
        "az": az,
        "efs_id": efs_id,
        "fullname_sanitized": fullname_sanitized,
        "public_key": request.user["SSHPublicKey"],
        "region": region,
        "user_id": request.user["UserId"],
    }

    run_kw = dict(
        ImageId=ami,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": int(volume_size),
                    "DeleteOnTermination": True,
                    "VolumeType": "gp2",
                }
            }
        ],
        Monitoring={"Enabled": True},
        NetworkInterfaces=[
            {
                "DeviceIndex": 0,
                "SubnetId": subnet,
                "AssociatePublicIpAddress": True,
                "Groups": [security_group],
                "DeleteOnTermination": True,
            }
        ],
        UserData=user_info,
    )

    if admin_ssh_key:
        run_kw["KeyName"] = admin_ssh_key

    response = ec2.run_instances(**run_kw)
    instance_id = response["Instances"][0]["InstanceId"]

    ec2.create_tags(Resources=[instance_id], Tags=[
        {
            "Key": "Name",
            "Value": ("%s instance for %s" %
                      (request.user["EventId"], request.user["Email"]))
        },
        {
            "Key": "HPCLab EventId",
            "Value": request.user["EventId"]
        },
        {
            "Key": "HPCLab UserEmail",
            "Value": request.user["Email"]
        },
        {
            "Key": "HPCLab NumericUserId",
            "Value": str(request.user["UserId"])
        },
    ])

    last_exception = None
    for retry in range(n_retries):
        try:
            ddb_users.update_item(
                Key={
                    "Email": request.user["Email"],
                    "EventId": request.user["EventId"],
                },
                UpdateExpression="SET InstanceId = :instance_id",
                ExpressionAttributeValues={":instance_id": instance_id},
            )
            return redirect("/")
        except BotoCoreError as e:
            print(str(e), file=stderr)
            sleep(2)
            last_exception = e
            continue

    flash("<b>Failed to record instance launch:</b> %s" %
          escape(last_exception), category="error")
    return redirect("/")


def ec2_terminate():
    # Make sure the user has an EC2 instance.
    instance_id = request.user.get("InstanceId")
    if not instance_id:
        flash("You do not have an EC2 instance assigned.", category="info")
        return redirect("/")

    ec2.terminate_instances(InstanceIds=[instance_id])
    ec2_clear_user_instance()
    return redirect("/")


def ec2_start():
    # Make sure the user has an EC2 instance.
    instance_id = request.user.get("InstanceId")
    if not instance_id:
        flash("You do not have an EC2 instance assigned.", category="info")
        return redirect("/")

    ec2.start_instances(InstanceIds=[instance_id])
    flash("Instance started.", category="info")
    return redirect("/")


def ec2_stop():
    # Make sure the user has an EC2 instance.
    instance_id = request.user.get("InstanceId")
    if not instance_id:
        flash("You do not have an EC2 instance assigned.", category="info")
        return redirect("/")

    ec2.stop_instances(InstanceIds=[instance_id])
    flash("Instance stopped.", category="info")
    return redirect("/")


def ec2_reboot():
    # Make sure the user has an EC2 instance.
    instance_id = request.user.get("InstanceId")
    if not instance_id:
        flash("You do not have an EC2 instance assigned.", category="info")
        return redirect("/")

    ec2.reboot_instances(InstanceIds=[instance_id])
    flash('Reboot signal sent to instance.<br><div class="hint">This is'
          'equivalent to pressing Ctrl+Alt+Delete. If the instance hasn\'t '
          'rebooted in four minutes, a hard reset will be issued.</div>',
          category="info")
    return redirect("/")


@app.route("/ssh-key", methods=["GET"])
@require_valid_session
def get_ssh_key(**kw):
    format = request.args.get("format", "PEM")
    priv_key = request.user["SSHPrivateKey"]
    event_id = request.user["EventId"]

    headers = {
        "Cache-Control": "private"
    }

    if format == "PEM":
        result = priv_key
        headers["Content-Type"] = "application/x-pem-file"
        headers["Content-Disposition"] = (
            'attachment; filename="%s-private.pem"' % (event_id,))
    elif format == "PPK":
        # Convert this to a PuTTY PPK file using puttygen. Note that puttygen
        # reopens the incoming PEM file, so /dev/stdin can't be used here.
        puttygen = app.config.get("PUTTYGEN", "/usr/bin/puttygen")
        temp_pem, temp_pem_filename = mkstemp(
            suffix=".pem", prefix="privkey", text=True)
        write(temp_pem, priv_key)
        fsync(temp_pem)

        print([puttygen, temp_pem, "-o", "/dev/stdout"])
        proc = Popen([puttygen, temp_pem_filename, "-o", "/dev/stdout"],
                     stdin=PIPE, stdout=PIPE, stderr=PIPE)
        ppk, err = proc.communicate()
        if proc.returncode != 0:
            raise ValueError("puttygen failed to convert PEM file: %s" %
                             err.strip())

        close(temp_pem)

        result = ppk
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Disposition"] = 'attachment; filename="%s.ppk"' % (
            event_id,)

    return make_response((result, OK, headers))


@app.route("/login", methods=["GET"])
def login(**kw):
    return render_template("login.html", form={})


@app.route("/login", methods=["POST"])
def login_post(**kw):
    action = request.form.get("Action")
    event_id = request.form.get("EventId")
    email = request.form.get("Email")
    full_name = request.form.get("FullName")
    password = request.form.get("Password")
    action = request.form.get("Action")
    password_verify = request.form.get("PasswordVerify")
    allow_contact = request.form.get("AllowContact")

    def redo(status_code):
        return make_response(
            render_template("login.html", form=request.form), status_code)

    if action == "Login":
        if event_id is None or email is None or password is None:
            flash("<b>Missing form fields</b>", category="error")
            return redo(BAD_REQUEST)

        if not is_valid_event_id(event_id):
            flash("<b>Unknown event code %s</b>" % escape(event_id),
                  category="error")
            return redo(UNAUTHORIZED)

        user = login_user(email, password, event_id)
        if not user:
            flash("<b>Invalid username or password</b>", category="error")
            return redo(UNAUTHORIZED)

        next = request.args.get("next")
        return redirect(next or "/")
    elif action == "Register":
        if (event_id is None or email is None or password is None or
            password_verify is None or full_name is None):
            flash("<b>Missing form fields</b>")
            return redo(BAD_REQUEST)

        if not event_id:
            flash("<b>Missing event code</b>", category="error")
            return redo(BAD_REQUEST)

        if not is_valid_event_id(event_id):
            flash("<b>Unknown event code %s</b>" % escape(event_id),
                  category="error")
            return redo(UNAUTHORIZED)

        if not validate_email(email):
            flash("<b>Invalid email address</b>", category="error")
            return redo(BAD_REQUEST)

        password_errors = []
        if len(password) < 8:
            password_errors.append("Password is too short.")

        upper_seen = lower_seen = digit_seen = symbol_seen = False
        for c in password:
            upper_seen |= c.isupper()
            lower_seen |= c.islower()
            digit_seen |= c.isdigit()
            symbol_seen |= not(c.isupper() and c.islower() and c.isdigit())

        if not upper_seen:
            password_errors.append(
                "Password does not contain an uppercase letter.")

        if not lower_seen:
            password_errors.append(
                "Password does not contain a lowercase letter.")

        if not digit_seen:
            password_errors.append("Password does not contain a digit.")

        if not symbol_seen:
            password_errors.append("Password does not contain a symbol.")

        try:
            if not password_errors:
                VeryFascistCheck(password)
        except ValueError:
            password_errors.append(
                "Password is easily guessed (was guessed by "
                "<a href=\"https://www.cyberciti.biz/security/"
                "linux-password-strength-checker/\">Cracklib</a>).")

        if password != password_verify:
            password_errors.append("Passwords do not match.")

        if password_errors:
            flash("<b>Invalid password:</b><br>" +
                  "<br>".join(password_errors),
                  category="error")
            return redo(BAD_REQUEST)

        user = register_user(
            email, password, event_id, full_name, allow_contact)

        if user is None:
            flash("<b>User is already registered. "
                  "<a href='/forgot-password'>Click here</a> to reset your "
                  "password.</b>", category="error")
            return redo(BAD_REQUEST)

        return redirect("/")
    else:
        flash("<b>Invalid form data sent</b>", category="error")
        return redo(BAD_REQUEST)


@app.route("/logout", methods=["GET", "POST"])
def logout(**kw):
    if "Email" in session:
        del session["Email"]
        session.modified = True

    if "EventId" in session:
        del session["EventId"]
        session.modified = True

    if session.modified:
        flash("<b>You have been logged out.</b>", category="info")
    return redirect("/login")


def handle_one_time_password_generation(event):
    request_type = event["RequestType"]

    if request_type in ["Create", "Update"]:
        # Generate a one-time password and save it, encrypted, in the database.
        otp = b58encode(urandom(20))[:20]
        otp_hash = pbkdf2_sha512.encrypt(
            otp, rounds=app.config["PBKDF2_SHA512_ROUNDS"])

        ddb_events.update_item(
            Key={"EventId": "_"},
            UpdateExpression="SET OneTimePasswordHash = :otp",
            ExpressionAttributeValues={":otp": otp_hash}
        )
        return {
            "Password": otp,
            "PhysicalResourceId": "password",
        }
    elif request_type == "Delete":
        # Delete the one-time password if it exists.
        ddb_events.update_item(
            Key={"EventId": "_"},
            UpdateExpression="REMOVE OneTimePasswordHash",
            ConditionExpression=AttrCondition("OneTimePasswordHash").exists()
        )
        return {}
    else:
        raise RuntimeError("Unknown request type %s" % request_type)


def handler(event, context):
    cfn_resource_type = event.get("ResourceType")
    if cfn_resource_type is None:
        # Process this as a standard API Gateway request.
        try:
            return lambda_handler(event, context)
        except Exception as e:
            from traceback import print_exc
            print_exc()
            raise

    # CloudFormation custom resource
    status = "FAILED"
    reason = None
    request_type = event["RequestType"]
    response_url = event["ResponseURL"]
    stack_id = event["StackId"]
    request_id = event["RequestId"]
    stack_name = event["ResourceProperties"]["StackName"]
    logical_resource_id = event["LogicalResourceId"]
    physical_resource_id = event.get(
        "PhysicalResourceId", stack_name + "-" + logical_resource_id)
    stack_name = event["StackName"]
    result = {}

    print("Handling CloudFormation custom resource: %s %s" % (
        request_type, cfn_resource_type))

    try:
        if cfn_resource_type == "Custom::OneTimePasswordGeneration":
            result = handle_one_time_password_generation(event)
            status = "SUCCESS"
        elif cfn_resource_type == "Custom::SiteURLRetrieval":
            result = handle_site_url_retrieval(event)
            status = "SUCCESS"
        else:
            status = "FAILED"
            reason = "Unknown resource type %s" % (cfn_resource_type,)
    except Exception as e:
        status = "FAILED"
        reason = "Custom resource event failed: %s" % (e,)

    physical_resource_id = result.get(
        "PhysicalResourceId", physical_resource_id)

    response = {
        "Status": status,
        "PhysicalResourceId": physical_resource_id,
        "StackId": stack_id,
        "RequestId": request_id,
        "LogicalResourceId": logical_resource_id,
        "Data": result,
    }

    if reason:
        response["Reason"] = reason

    r = requests.put(response_url, headers=headers, data=body)
    print("Result: %d %s" % (r.status_code, r.reason))
    return
