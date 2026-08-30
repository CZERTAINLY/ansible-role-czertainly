#!/usr/bin/python3
"""Check that a mail server accepts the settings of the email notification provider.

The provider runs Spring Boot with 'spring.mail.test-connection: true', so it
connects to the mail server and authenticates while its context starts. Wrong
settings do not degrade a feature, they kill the container: it exits, the pod
goes to CrashLoopBackOff, and 'helm --wait' reports a timeout that says nothing
about mail. This does the same exchange first, in a second, and says what the
mail server answered.

Nothing is sent unless --send-to is given.

The exchange is deliberately the one JavaMail performs with the settings the
chart gets, and not a stricter one:

  * STARTTLS is opportunistic. The provider sets mail.smtp.starttls.enable but
    not starttls.required, so it upgrades only when the server advertises
    STARTTLS and otherwise carries on in the clear. So does this, with a
    warning.
  * The certificate is verified. Jakarta Mail checks the chain and the server
    identity, against a truststore the appliance builds from its own trusted
    certificates. Pass those with --ca-file, they are added to the CAs of the
    system.

Exit codes: 0 accepted, 1 usage, 2 connection, 3 TLS, 4 authentication.
"""

import argparse
import os
import smtplib
import socket
import ssl
import sys

EX_OK = 0
EX_USAGE = 1
EX_CONNECT = 2
EX_TLS = 3
EX_AUTH = 4

PROG = os.path.basename(sys.argv[0])

# Our own messages go here, so that they are not read as a line of the
# conversation while --verbose has sys.stderr replaced.
REAL_STDERR = sys.stderr


def fail(code, message):
    print("%s: %s" % (PROG, message), file=REAL_STDERR)
    sys.exit(code)


def warn(message):
    print("%s: warning: %s" % (PROG, message), file=REAL_STDERR)


def smtp_error(exc):
    """The text the server sent, out of one of smtplib's exceptions."""

    error = getattr(exc, "smtp_error", None)
    if isinstance(error, bytes):
        error = error.decode("utf-8", "replace")
    code = getattr(exc, "smtp_code", None)
    if error and code:
        return "%s %s" % (code, error.replace("\n", " "))
    return error or str(exc)


class RedactingStderr:
    """sys.stderr for the time of the session, with the credentials taken out.

    smtplib's debug output prints every line of the conversation, and the
    AUTH command and its continuations carry the password base64 encoded.
    Anything sent between AUTH and the reply that ends it is replaced.

    print() writes a line in several pieces - 'send:', ' ', the payload, the
    newline - so the pieces are collected and only whole lines are looked at.
    """

    def __init__(self, stream):
        self.stream = stream
        self.in_auth = False
        self.buffer = ""

    def write(self, data):
        self.buffer += data
        while "\n" in self.buffer:
            line, _, self.buffer = self.buffer.partition("\n")
            self.stream.write(self._redact(line) + "\n")

    def _redact(self, line):
        stripped = line.strip()
        if stripped.startswith("send:") and "AUTH" in stripped.upper():
            self.in_auth = True
            head, _, _ = line.partition("AUTH")
            # smtplib prints the payload repr()ed, so close the quote again.
            quote = "'" if head.rstrip().endswith(("'", '"')) else ""
            return "%sAUTH <redacted>%s" % (head, quote)
        if self.in_auth:
            if stripped.startswith("send:"):
                return "send: <redacted>"
            # 334 is the server asking for the next part of the exchange,
            # anything else ends it.
            if stripped.startswith("reply:") and "334" not in stripped:
                self.in_auth = False
        return line

    def flush(self):
        if self.buffer:
            self.stream.write(self._redact(self.buffer))
            self.buffer = ""
        self.stream.flush()


def unreachable(host, port, timeout):
    """Why every address of the host was unreachable.

    smtplib reports only the error of the last address it tried, which on a
    host without IPv6 is the IPv6 one - 'Address family not supported by
    protocol' for a port that is simply closed. Ask every address instead.
    """

    reasons = []
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return str(exc)
    for family, socktype, proto, _, address in addresses:
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(address)
            sock.close()
            reasons.append("%s: connected" % address[0])
        except OSError as exc:
            reasons.append("%s: %s" % (address[0], exc.strerror or exc))
    return "; ".join(reasons)


def connect(host, port, timeout):
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        fail(EX_CONNECT, "cannot resolve %s: %s" % (host, exc.strerror or exc))

    try:
        return smtplib.SMTP(host, port, timeout=timeout)
    except (OSError, smtplib.SMTPException) as exc:
        fail(
            EX_CONNECT,
            "cannot connect to %s:%s: %s"
            % (host, port, unreachable(host, port, timeout) or exc),
        )


def start_tls(smtp, host, port, ca_file):
    if not smtp.has_extn("starttls"):
        warn(
            "%s:%s does not offer STARTTLS, continuing unencrypted - the "
            "provider does the same, its starttls.enable only upgrades when "
            "the server offers it" % (host, port)
        )
        return False

    context = ssl.create_default_context()
    if ca_file:
        try:
            context.load_verify_locations(cafile=ca_file)
        except OSError as exc:
            fail(EX_USAGE, "cannot read the certificates of %s: %s" % (ca_file, exc))

    try:
        smtp.starttls(context=context)
    except ssl.SSLCertVerificationError as exc:
        fail(
            EX_TLS,
            "the certificate of %s:%s is not trusted: %s - the provider "
            "validates it as well, add the issuing CA to "
            "/etc/ilm-ansible/vars/trustedCA.yml" % (host, port, exc.verify_message or exc),
        )
    except (ssl.SSLError, OSError, smtplib.SMTPException) as exc:
        fail(EX_TLS, "STARTTLS with %s:%s failed: %s" % (host, port, smtp_error(exc)))

    smtp.ehlo()
    return True


def authenticate(smtp, host, port, user, password):
    if not smtp.has_extn("auth"):
        fail(
            EX_AUTH,
            "%s:%s offers no authentication, so the credentials of %s cannot "
            "be used - leave the username empty or connect to a port that "
            "does" % (host, port, user),
        )

    try:
        smtp.login(user, password)
    except smtplib.SMTPAuthenticationError as exc:
        fail(
            EX_AUTH,
            "%s:%s rejected the credentials of %s: %s"
            % (host, port, user, smtp_error(exc)),
        )
    except smtplib.SMTPException as exc:
        fail(
            EX_AUTH,
            "%s:%s did not authenticate %s: %s" % (host, port, user, smtp_error(exc)),
        )


def send(smtp, host, port, sender, recipient):
    message = (
        "From: %s\r\n"
        "To: %s\r\n"
        "Subject: ILM appliance mail server test\r\n"
        "\r\n"
        "Sent by %s from %s to check the settings of the email notification "
        "provider.\r\n" % (sender, recipient, PROG, socket.getfqdn())
    )
    try:
        smtp.sendmail(sender, [recipient], message)
    except smtplib.SMTPException as exc:
        fail(
            EX_CONNECT,
            "%s:%s accepted the settings but refused a message from %s to %s: %s"
            % (host, port, sender, recipient, smtp_error(exc)),
        )
    print("sent a test message from %s to %s" % (sender, recipient))


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Check that a mail server accepts the settings of the "
        "ILM email notification provider. Connects, offers STARTTLS and "
        "authenticates, the way the provider does while it starts. Sends "
        "nothing unless --send-to is given.",
        epilog="Exit codes: 0 accepted, 1 usage, 2 connection, 3 TLS, "
        "4 authentication. The password is taken from $SMTP_PASSWORD when it "
        "is set, so that it stays out of the process list.",
    )
    parser.add_argument("--host", required=True, help="mail server to contact")
    parser.add_argument(
        "--port", type=int, default=587, help="port to contact, 587 by default"
    )
    parser.add_argument(
        "--tls",
        dest="tls",
        action="store_true",
        default=True,
        help="offer STARTTLS, the default",
    )
    parser.add_argument(
        "--no-tls", dest="tls", action="store_false", help="do not offer STARTTLS"
    )
    parser.add_argument("--user", help="username to authenticate with")
    parser.add_argument(
        "--password", help="password, $SMTP_PASSWORD is used when it is set"
    )
    parser.add_argument(
        "--ca-file",
        help="certificates to trust besides those of the system, the file of "
        "trustedCA_file",
    )
    parser.add_argument(
        "--timeout", type=float, default=10, help="seconds to wait, 10 by default"
    )
    parser.add_argument(
        "--send-to",
        metavar="ADDRESS",
        help="also send a test message there, off by default",
    )
    parser.add_argument("--from", dest="sender", metavar="ADDRESS", help="its sender")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print the conversation, without the credentials",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    password = os.environ.get("SMTP_PASSWORD") or args.password
    if args.user and not password:
        fail(EX_USAGE, "--user needs a password, in --password or $SMTP_PASSWORD")
    if args.send_to and not args.sender:
        args.sender = (
            args.user if args.user and "@" in args.user else "ilm@%s" % socket.getfqdn()
        )

    smtp = connect(args.host, args.port, args.timeout)

    if args.verbose:
        sys.stderr = RedactingStderr(REAL_STDERR)
        smtp.set_debuglevel(1)

    try:
        smtp.ehlo()
        encrypted = start_tls(smtp, args.host, args.port, args.ca_file) if args.tls else False
        if args.user:
            authenticate(smtp, args.host, args.port, args.user, password)
        if args.send_to:
            send(smtp, args.host, args.port, args.sender, args.send_to)
        try:
            smtp.quit()
        except smtplib.SMTPException:
            smtp.close()
    finally:
        sys.stderr.flush()
        sys.stderr = REAL_STDERR

    print(
        "%s:%s accepted the settings (%s, %s)"
        % (
            args.host,
            args.port,
            "STARTTLS" if encrypted else "unencrypted",
            "authenticated as %s" % args.user if args.user else "no authentication",
        )
    )
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
