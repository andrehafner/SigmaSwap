package MosaikKit;

# Shared building blocks for the Mosaik apps in this repository (SigmaSwap, SigmaMint):
#   - a tiny JSON encoder with ORDERED objects (Mosaik drop-down lists are JSON
#     objects whose key order is the display order, which JSON::PP cannot guarantee)
#   - constructors for the common Mosaik view elements
#   - the small on-disk store (CACHE_DIR) for short-lived notes keyed by a random id

use strict;
use warnings;
use Exporter 'import';
use File::Spec;
use JSON::PP ();
use Digest::SHA qw(sha256_hex);
use AppConfig qw(cfg chain);

my $ERG_ID = '0' x 64;

our @EXPORT = qw(get_pools registry watch_add notifications notify_json order_register O N TRUE FALSE enc enc_str out_json die_json success_json fmt_amount cache_dir label child column row card button kv_row new_nonce record_file save_record load_record);

sub O     { my @kv = @_; my @o; while (@kv) { my ($k, $v) = splice(@kv, 0, 2); push @o, $k, $v if defined $v } return bless \@o, 'JObj' }

sub N     { my $n = "$_[0]"; die "not a number\n" unless $n =~ /^-?\d+(\.\d+)?\z/; return bless \$n, 'JNum' }

sub TRUE  { my $b = 1; return bless \$b, 'JBool' }

sub FALSE { my $b = 0; return bless \$b, 'JBool' }

sub enc {
    my ($v) = @_;
    return 'null' unless defined $v;
    my $r = ref $v;
    if ($r eq 'JObj') {
        my @p;
        for (my $i = 0; $i < @$v; $i += 2) { push @p, enc_str($v->[$i]) . ':' . enc($v->[ $i + 1 ]) }
        return '{' . join(',', @p) . '}';
    }
    return '[' . join(',', map { enc($_) } @$v) . ']' if $r eq 'ARRAY';
    return '{' . join(',', map { enc_str($_) . ':' . enc($v->{$_}) } sort keys %$v) . '}' if $r eq 'HASH';
    return $$v if $r eq 'JNum';
    return $$v ? 'true' : 'false' if $r eq 'JBool';
    return enc_str($v);
}

sub enc_str {
    my ($s) = @_;
    $s = "$s";
    $s =~ s/(["\\])/\\$1/g;
    $s =~ s/([\x00-\x1f\x7f])/sprintf('\\u%04x', ord $1)/ge;
    # everything non-ASCII as \u escapes (surrogate pairs above the BMP): output is pure ASCII
    $s =~ s/([^\x00-\x7e])/
        my $c = ord $1;
        $c > 0xFFFF
            ? sprintf('\\u%04x\\u%04x', 0xD800 + (($c - 0x10000) >> 10), 0xDC00 + (($c - 0x10000) & 0x3FF))
            : sprintf('\\u%04x', $c)
    /ge;
    return qq{"$s"};
}

sub out_json     { print enc($_[0]) }

sub die_json     { out_json(O(success => N(0), error => $_[0])); exit }

sub success_json { my @kv = @_; out_json(O(success => N(1), @kv)) }

sub fmt_amount {
    my ($raw, $dec, $max_frac) = @_;
    $raw = "$raw";
    return '0' unless $raw =~ /^\d+\z/;
    $raw = ('0' x ($dec + 1 - length $raw)) . $raw if length($raw) <= $dec;
    my $int  = substr($raw, 0, length($raw) - $dec);
    my $frac = $dec ? substr($raw, -$dec) : '';
    $frac = substr($frac, 0, $max_frac) if defined $max_frac && length($frac) > $max_frac;
    $frac =~ s/0+\z//;
    $int =~ s/^0+(?=\d)//;
    1 while $int =~ s/^(\d+)(\d{3})/$1,$2/;
    return length($frac) ? "$int.$frac" : $int;
}

sub cache_dir {
    my $dir = cfg('CACHE_DIR', File::Spec->catdir(File::Spec->tmpdir, "sigmaswap-cache-$>"));
    mkdir $dir, 0770 unless -d $dir;
    return (-d $dir && -w $dir) ? $dir : undef;
}

sub label {
    my ($text, $style, %o) = @_;
    return O(type => 'Label', id => $o{id}, text => $text, style => $style || 'BODY1',
             textColor => $o{color}, textAlignment => $o{talign}, maxLines => $o{lines} ? N($o{lines}) : undef);
}

sub child { my ($el, %layout) = @_; push @$el, map { $_ => $layout{$_} } sort keys %layout; return $el }

sub column {
    my ($children, %o) = @_;
    return O(type => 'Column', id => $o{id}, padding => $o{padding}, spacing => $o{spacing}, children => $children);
}

sub row  { my ($children, %o) = @_; return O(type => 'Row', id => $o{id}, spacing => $o{spacing}, children => $children) }

sub card { my ($content, %o) = @_; return O(type => 'Card', id => $o{id}, padding => $o{padding}, children => [$content]) }

sub button {
    my ($text, $on_click, %o) = @_;
    return O(type => 'Button', id => $o{id}, onClick => $on_click, text => $text,
             enabled => (exists $o{enabled} && !$o{enabled}) ? FALSE : undef, style => $o{style});
}

sub kv_row {    # "Label ........ value"
    my ($k, $v, %o) = @_;
    my $value = ref $v ? $v : label($v, $o{style} || 'BODY1BOLD', color => $o{color}, talign => 'END');
    return child(row([ child(label($k, 'BODY1', color => 'SECONDARY'), weight => N(1)), $value ]), align => 'JUSTIFY');
}

sub new_nonce {
    open(my $fh, '<:raw', '/dev/urandom') or return undef;
    my $n = read($fh, my $buf, 16);
    close $fh;
    return (defined $n && $n == 16) ? unpack('H*', $buf) : undef;
}

sub record_file {
    my ($nonce) = @_;
    return undef unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/;
    my $base = cache_dir() or return undef;
    my $dir = File::Spec->catdir($base, 'tx');
    mkdir $dir, 0770 unless -d $dir;
    return (-d $dir && -w $dir) ? File::Spec->catfile($dir, "$nonce.json") : undef;
}

sub save_record {
    my ($nonce, $rec) = @_;
    my $file = record_file($nonce) or return;
    my $tmp = "$file.$$";
    if (open(my $fh, '>', $tmp)) {
        print {$fh} JSON::PP->new->utf8->encode($rec);
        close $fh;
        rename($tmp, $file) or unlink $tmp;
    }
    # housekeeping, now and then: records are only useful for a day or two
    if (int(rand(25)) == 0 && opendir(my $dh, File::Spec->catdir(cache_dir(), 'tx'))) {
        for my $f (grep { /^[0-9a-f]{32}\.json\z/ } readdir $dh) {
            my $p = File::Spec->catfile(cache_dir(), 'tx', $f);
            unlink $p if time - ((stat $p)[9] || time) > 2 * 86400;
        }
        closedir $dh;
    }
}

sub load_record {
    my ($nonce) = @_;
    my $file = record_file($nonce) or return undef;
    open(my $fh, '<', $file) or return undef;
    local $/;
    my $d = eval { JSON::PP->new->utf8->decode(scalar <$fh>) };
    close $fh;
    return ref $d eq 'HASH' ? $d : undef;
}

# ---- the liquid-pool list (shared by SigmaSwap and SigmaLP) -------------------

sub cache_file {
    my $dir = cache_dir();
    return defined $dir ? File::Spec->catfile($dir, 'pools.json') : undef;
}

sub read_pool_cache {
    my ($file) = @_;
    open(my $fh, '<', $file) or return undef;
    local $/;
    my $d = eval { JSON::PP->new->utf8->decode(scalar <$fh>) };
    close $fh;
    return (ref $d eq 'HASH' && ref $d->{pools} eq 'ARRAY' && @{ $d->{pools} }) ? $d->{pools} : undef;
}

sub get_pools {
    my $file = cache_file();
    my $ttl  = cfg('POOLS_TTL', 180) + 0;
    my $have = defined $file && -f $file;
    if ($have && time - (stat $file)[9] < $ttl) {
        my $p = read_pool_cache($file);
        return $p if $p;
    }
    my $res = chain('pools', {});
    if ($res->{ok} && ref $res->{pools} eq 'ARRAY' && @{ $res->{pools} }) {
        if (defined $file) {
            my $tmp = "$file.$$";
            if (open(my $fh, '>', $tmp)) {
                print {$fh} JSON::PP->new->utf8->encode({ pools => $res->{pools} });
                close $fh;
                rename($tmp, $file) or unlink $tmp;
            }
        }
        return $res->{pools};
    }
    if ($have) {    # upstream is down: a stale list beats no app (quotes are always live)
        my $p = read_pool_cache($file);
        return $p if $p;
    }
    return [];
}

# -> { tokens => {id => {id,ticker,decimals,tvl,label}}, order => [ids], pair => {a => {b => pool}} }
sub registry {
    my $pools = get_pools();
    my (%tok, %pair, %seen);
    for my $p (@$pools) {
        for my $side ($p->{x}, $p->{y}) {
            my $t = $tok{ $side->{id} } ||= { id => $side->{id}, ticker => $side->{ticker},
                                               decimals => $side->{decimals} + 0, tvl => 0 };
            $t->{tvl} += $p->{tvl};
        }
        $pair{ $p->{x}{id} }{ $p->{y}{id} } = $p;
        $pair{ $p->{y}{id} }{ $p->{x}{id} } = $p;
    }
    # Token names are free text chosen by whoever minted the token, so two tokens
    # can share a ticker. Whenever that happens, show the token id next to it.
    $seen{ lc $_->{ticker} }++ for values %tok;
    for my $t (values %tok) {
        $t->{label} = ($seen{ lc $t->{ticker} } > 1 && $t->{id} ne $ERG_ID)
            ? "$t->{ticker} (" . substr($t->{id}, 0, 6) . "\x{2026})" : $t->{ticker};
    }
    my @order = sort { ($b eq $ERG_ID) <=> ($a eq $ERG_ID) || $tok{$b}{tvl} <=> $tok{$a}{tvl} || $a cmp $b } keys %tok;
    return { tokens => \%tok, order => \@order, pair => \%pair };
}

# ---- notifications -----------------------------------------------------------
# The wallet identifies an installation with a random id it sends on every Mosaik
# request (header Mosaik-guid; nothing personal). We keep, per installation, the
# short list of things it started (a swap, an order, a mint) so that the wallet's
# periodic "anything new?" check can be answered: "your order was filled".

sub _watch_file {
    my $g = $ENV{HTTP_MOSAIK_GUID};
    return undef unless defined $g && $g =~ /^[A-Za-z0-9._-]{8,80}\z/;
    my $cd = cache_dir() or return undef;
    my $dir = File::Spec->catdir($cd, 'watch');
    mkdir $dir, 0770 unless -d $dir;
    return (-d $dir && -w $dir) ? File::Spec->catfile($dir, substr(sha256_hex("guid|$g"), 0, 32) . '.json') : undef;
}

sub _watch_load {
    my ($file) = @_;
    my $w;
    if (open(my $fh, '<', $file)) {
        local $/;
        $w = eval { JSON::PP->new->utf8->decode(scalar <$fh>) };
        close $fh;
    }
    $w = {} unless ref $w eq 'HASH';
    $w->{items} = [] unless ref $w->{items} eq 'ARRAY';
    $w->{msg}   = {} unless ref $w->{msg} eq 'HASH';
    return $w;
}

sub _watch_save {
    my ($file, $w) = @_;
    my $tmp = "$file.$$";
    if (open(my $fh, '>', $tmp)) {
        print {$fh} JSON::PP->new->utf8->encode($w);
        close $fh;
        rename($tmp, $file) or unlink $tmp;
    }
}

# remember that this installation started something under note id $nonce
sub watch_add {
    my ($app, $nonce) = @_;
    my $file = _watch_file() or return;
    my $w = _watch_load($file);
    my @keep = grep { ref $_ eq 'HASH' && time - ($_->{t} || 0) < 60 * 86400 } @{ $w->{items} };
    unshift @keep, { app => $app, n => $nonce, t => time };
    splice(@keep, 40) if @keep > 40;
    $w->{items} = \@keep;
    _watch_save($file, $w);
}

# Answer the wallet's check for one app. $check->($item) returns (is_final, message);
# an item becomes final once and its message (if any) is what the wallet shows.
# -> (message or undef, timestamp, minutes until the next check)
sub notifications {
    my ($app, $check) = @_;
    my $file = _watch_file() or return (undef, 0, 240);
    my $w = _watch_load($file);
    my ($open, $changed, $looked) = (0, 0, 0);
    for my $it (@{ $w->{items} }) {
        next unless ref $it eq 'HASH' && ($it->{app} // '') eq $app && !$it->{done};
        if (!$it->{tx_id}) {    # has the wallet fetched the transaction yet?
            my $rec = load_record($it->{n});
            if ($rec && $rec->{tx_id}) {
                $it->{$_} = $rec->{$_} for grep { defined $rec->{$_} } keys %$rec;
                $changed = 1;
            }
            elsif (time - ($it->{t} || 0) > 3600) { $it->{done} = 1; $changed = 1; next }
            else                                   { $open++; next }
        }
        if (++$looked > 6) { $open++; next }    # bound the work of one check
        my ($final, $msg) = $check->($it);
        if ($final) {
            $it->{done} = 1;
            $w->{msg}{$app} = { text => $msg, ts => time } if defined $msg;
            $changed = 1;
        }
        else { $open++ }
    }
    _watch_save($file, $w) if $changed;
    my $m = $w->{msg}{$app};
    my $fresh = ref $m eq 'HASH' && defined $m->{text} && time - ($m->{ts} || 0) < 3 * 86400;
    return ($fresh ? $m->{text} : undef, $fresh ? $m->{ts} : 0, $open ? 5 : 240);
}

sub notify_json {    # Mosaik NotificationCheckResponse
    my ($text, $ts, $next) = @_;
    out_json(O(message => $text, messageTs => N(($ts || 0) * 1000), nextCheck => N($next)));
}

# ---- order registry: what the completing bot (sigmaswap.py bot) looks at
sub order_register {
    my ($box_id, $address) = @_;
    return unless defined $box_id && $box_id =~ /^[0-9a-f]{64}\z/;
    my $cd = cache_dir() or return;
    my $dir = File::Spec->catdir($cd, 'orders');
    mkdir $dir, 0770 unless -d $dir;
    return unless -d $dir && -w $dir;
    my $file = File::Spec->catfile($dir, "$box_id.json");
    return if -e $file;
    if (open(my $fh, '>', $file)) {
        print {$fh} JSON::PP->new->utf8->encode({ order_box => $box_id, address => $address, t => time });
        close $fh;
    }
}

1;
