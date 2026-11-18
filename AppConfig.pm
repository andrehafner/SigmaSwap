package AppConfig;

# Shared config/helper module for SigmaSwap.
#
#   cfg(KEY, default)      any key from the app env file (APP_ENV_FILE)
#   base_url()             public https base of this app, no trailing slash
#   chain(cmd, \%request)  run the Python chain helper (sigmaswap.py) and return its decoded JSON
#
# SigmaSwap is a public, unauthenticated app with no database: there is no
# current_netid()/db_creds()/is_admin() here on purpose (see App_Summary.md).

use strict;
use warnings;
use Exporter 'import';
use FindBin;
use IPC::Open2;
use JSON::PP ();

our @EXPORT_OK = qw(cfg base_url chain);

my %ENVFILE;
my $loaded = 0;

sub _load {
    return if $loaded;
    $loaded = 1;
    my $file = $ENV{APP_ENV_FILE} or return;
    open(my $fh, '<', $file) or do {
        warn "AppConfig: cannot read APP_ENV_FILE ($!)\n";    # never print the path's contents
        return;
    };
    while (my $line = <$fh>) {
        $line =~ s/\r?\n\z//;
        next if $line =~ /^\s*(#|$)/;
        next unless $line =~ /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/;
        my ($k, $v) = ($1, $2);
        $v =~ s/^(["'])(.*)\1$/$2/;
        $ENVFILE{$k} = $v;
    }
    close $fh;
}

sub cfg {
    my ($key, $default) = @_;
    _load();
    return (defined $ENVFILE{$key} && $ENVFILE{$key} ne '') ? $ENVFILE{$key} : $default;
}

# Public base URL of the app directory, e.g. https://public.aap.cornell.edu/apps/sigmaswap
# Derived from the request so the same checkout serves either vhost; BASE_URL overrides it.
sub base_url {
    my $base = cfg('BASE_URL');
    if (!$base) {
        my $host = $ENV{HTTP_HOST} || $ENV{SERVER_NAME} || 'localhost';
        $host = 'localhost' unless $host =~ /^[A-Za-z0-9.\-]+(:\d{1,5})?$/;
        my $https = (($ENV{HTTPS} || '') =~ /^(on|1)$/i) || (($ENV{HTTP_X_FORWARDED_PROTO} || '') eq 'https');
        my $dir = $ENV{SCRIPT_NAME} || '';
        $dir =~ s{/[^/]*$}{};
        $dir = '' unless $dir =~ m{^(/[A-Za-z0-9._\-]+)*$};
        $base = ($https ? 'https' : 'http') . "://$host$dir";
    }
    $base =~ s{/+$}{};
    return $base;
}

# Run sigmaswap.py with a shell-free, list-form exec. The request goes in as JSON
# on stdin and the response is CAPTURED from stdout (it can never leak into the
# CGI response). The helper's stderr goes to the Apache error log.
sub chain {
    my ($cmd, $req) = @_;
    die "bad helper command\n" unless $cmd =~ /^[a-z]+$/;
    my %req = %{ $req || {} };
    $req{cfg} = {
        explorer_url  => cfg('EXPLORER_URL',  'https://api.ergoplatform.com'),
        pools_api_url => cfg('POOLS_API_URL', 'https://api.ergodex.io/v1/amm/pools/stats'),
        min_tvl_usd   => cfg('MIN_TVL_USD',   100) + 0,
        miner_fee     => cfg('MINER_FEE',     1100000) + 0,
    };
    my $python = cfg('PYTHON', "$FindBin::Bin/venv/bin/python");
    my $script = "$FindBin::Bin/sigmaswap.py";
    my $timeout = cfg('HELPER_TIMEOUT', 40) + 0;

    my ($out, $pid) = ('', undef);
    my $ok = eval {
        local $SIG{ALRM} = sub { die "timeout\n" };
        local $SIG{PIPE} = 'IGNORE';
        alarm $timeout;
        $pid = open2(my $rd, my $wr, $python, '-I', '-B', $script, $cmd);
        print {$wr} JSON::PP->new->utf8->encode(\%req);
        close $wr;
        local $/;
        $out = <$rd>;
        close $rd;
        waitpid($pid, 0);
        alarm 0;
        1;
    };
    if (!$ok) {
        my $err = $@ || 'unknown';
        alarm 0;
        if ($pid) { kill 'KILL', $pid; waitpid($pid, 0); }
        chomp $err;
        warn "sigmaswap: helper '$cmd' failed: $err\n";
        return { ok => 0, error => 'The service is busy. Please try again.' };
    }
    my $res = eval { JSON::PP->new->utf8->decode($out // '') };
    if (ref $res ne 'HASH') {
        warn "sigmaswap: helper '$cmd' returned no JSON (exit " . ($? >> 8) . ")\n";
        return { ok => 0, error => 'The service is temporarily unavailable.' };
    }
    return $res;
}

1;
