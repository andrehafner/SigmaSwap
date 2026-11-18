#!/usr/bin/perl
# SigmaSwap - Mosaik swap app for the ErgoDex liquidity pools.
#
# One action-routed CGI. It speaks three small protocols:
#
#   Mosaik (Ergo wallet in-app dApps)
#     GET  api.pl                    -> MosaikApp JSON (the whole view)
#     POST api.pl?action=refresh     -> re-render the form from the posted input values
#     POST api.pl?action=flip        -> same, with pay/receive tokens exchanged
#     POST api.pl?action=quote       -> price the swap, show details + Swap button
#     POST api.pl?action=orders      -> list the address's open swap orders (cancel buttons)
#     POST api.pl?action=home        -> the start screen (choose direct swap or order)
#     POST api.pl?action=status&n=   -> what happened to the swap/order just signed
#   (&mode=direct|order on every Mosaik action says which of the two screens is open)
#   ErgoPay (EIP-20), fetched by the wallet when the user taps Swap
#     GET  api.pl?action=ergopay&... -> { reducedTx, address, message, messageSeverity }
#          (a direct swap; with &o=1 an order; with &r=<box id> the refund of an order)
#   Landing page (index.html / app.js), POST FormData like every other AAP app
#     POST api.pl  action=pools      -> { success: 1, pools: [...] }
#     POST api.pl  action=health     -> { success: 1, ... }   (deploy gate)
#
# The app is public: no login, no database, no cookies, no keys. The only thing it
# stores is a short-lived note of each transaction it built (public data), so it
# can tell the user afterwards whether the swap went through.
# The server only ever builds an UNSIGNED transaction; the user's wallet shows
# it, and signs it or not. All chain work happens in sigmaswap.py.

use strict;
use warnings;
use FindBin;
use lib $FindBin::Bin;
use CGI ();
use JSON::PP ();
use Math::BigInt;
use Math::BigFloat;
use File::Spec;
use AppConfig qw(cfg base_url chain);

use constant APP_VERSION => 5;     # bump when the Mosaik view changes: open wallets reload
use constant ERG         => '0' x 64;
use constant SLIPPAGE    => (50 => '0.5 %', 100 => '1 %', 300 => '3 %', 1000 => '10 %');

$CGI::POST_MAX        = 64 * 1024;
$CGI::DISABLE_UPLOADS = 1;

my $cgi = CGI->new;
# The JSON header MUST be the first thing printed.
print $cgi->header(-type => 'application/json', -charset => 'utf-8', -cache_control => 'no-store');

my $BASE = base_url();
my $API  = "$BASE/api.pl";

my $action = $cgi->url_param('action');
$action = $cgi->param('action') unless defined $action && $action ne '';
$action = '' unless defined $action;

# 'direct' or 'order': which of the two swap screens the user is on (carried in the action URLs)
my $MODE = $cgi->url_param('mode');
$MODE = 'direct' unless defined $MODE && $MODE eq 'order';

# Simple mode: the same swap with only the essentials on screen. It is chosen with
# the switch on the start screen (posted once as an input value) and from then on
# travels in the action URLs, like the mode.
my $SIMPLE = $cgi->url_param('simple');
if (defined $SIMPLE) { $SIMPLE = $SIMPLE eq '1' ? 1 : 0 }
else {
    my $v = posted_values();
    $SIMPLE = exists $v->{simple} ? ($v->{simple} ? 1 : 0) : (cfg('SIMPLE_DEFAULT', 0) ? 1 : 0);
}

my %actions = (
    ''        => \&act_app,
    'refresh' => \&act_refresh,
    'flip'    => \&act_flip,
    'quote'   => \&act_quote,
    'orders'  => \&act_orders,
    'home'    => \&act_home,
    'status'  => \&act_status,
    'ergopay' => \&act_ergopay,
    'pools'   => \&act_pools,
    'health'  => \&act_health,
);

if ($cgi->cgi_error) {
    die_json('Bad request');
}
elsif (exists $actions{$action}) {
    my $ok = eval { $actions{$action}->(); 1 };
    if (!$ok) {
        my $err = $@ || 'unknown error';
        $err =~ s/\s+\z//;
        warn "sigmaswap: action '$action' failed: $err\n";     # detail to the log only
        if    ($action eq 'ergopay')                  { ergopay_error('Something went wrong. Please try again.') }
        elsif ($action =~ /^(refresh|flip|quote|orders|home|status)\z/) { mosaik_dialog('Something went wrong. Please try again.') }
        else                                          { die_json('Something went wrong') }
    }
}
else {
    die_json('Unknown action');
}
exit;

# ===================================================================== JSON ==
# Tiny encoder with ORDERED objects: Mosaik drop-down lists are JSON objects
# whose key order is the display order, which JSON::PP cannot guarantee.

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

# ================================================================= amounts ==

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

sub fmt_rate {    # "1 FROM ~ x TO", six significant digits
    my ($in, $din, $out, $dout) = @_;
    return undef unless $in =~ /^\d+\z/ && $in > 0;
    my $r = Math::BigFloat->new("$out")->bmul(Math::BigFloat->new(10)->bpow($din))
        ->bdiv(Math::BigFloat->new("$in")->bmul(Math::BigFloat->new(10)->bpow($dout)), 40);
    return '0' if $r->is_zero;
    my $s = $r->bround(6)->bstr;
    $s =~ s/0+\z// if $s =~ /\./;
    $s =~ s/\.\z//;
    return $s;
}

sub fmt_pct {    # basis points -> "0.32 %"
    my ($bps) = @_;
    my $s = sprintf('%.2f', $bps / 100);
    $s =~ s/0+\z//;
    $s =~ s/\.\z//;
    return "$s %";
}

# =================================================================== pools ==
# The liquid-pool list is cached in a small JSON file (outside the app tree in
# production) so that opening the app does not hit the ErgoDex API every time.

sub cache_dir {
    my $dir = cfg('CACHE_DIR', File::Spec->catdir(File::Spec->tmpdir, "sigmaswap-cache-$>"));
    mkdir $dir, 0770 unless -d $dir;
    return (-d $dir && -w $dir) ? $dir : undef;
}

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
        $t->{label} = ($seen{ lc $t->{ticker} } > 1 && $t->{id} ne ERG)
            ? "$t->{ticker} (" . substr($t->{id}, 0, 6) . "\x{2026})" : $t->{ticker};
    }
    my @order = sort { ($b eq ERG) <=> ($a eq ERG) || $tok{$b}{tvl} <=> $tok{$a}{tvl} || $a cmp $b } keys %tok;
    return { tokens => \%tok, order => \@order, pair => \%pair };
}

# ============================================================ input values ==

sub posted_values {
    my $raw = $cgi->param('POSTDATA');
    return {} unless defined $raw && length $raw;
    my $v = eval { JSON::PP->new->utf8->decode($raw) };
    return ref $v eq 'HASH' ? $v : {};
}

# Everything the wallet posts is untrusted: keep only well-formed, known values.
sub clean_state {
    my ($v, $reg) = @_;
    my %s;
    my $addr = $v->{address};
    $s{address} = $addr if defined $addr && !ref $addr && $addr =~ /^[1-9A-HJ-NP-Za-km-z]{40,60}\z/;
    for my $k (qw(from to)) {
        my $id = $v->{$k};
        $s{$k} = $id if defined $id && !ref $id && exists $reg->{tokens}{$id};
    }
    $s{from} = ERG if !defined $s{from} && exists $reg->{tokens}{ +ERG };
    delete $s{to} if defined $s{to} && defined $s{from} && !$reg->{pair}{ $s{from} }{ $s{to} };
    if (defined $s{from}) {
        my $a = $v->{ 'amount_' . $reg->{tokens}{ $s{from} }{decimals} };
        $s{amount} = "$a" if defined $a && !ref $a && "$a" =~ /^[1-9]\d{0,18}\z/;
    }
    my %slip = SLIPPAGE;
    my $sl   = $v->{slippage};
    $s{slippage} = (defined $sl && !ref $sl && exists $slip{$sl}) ? $sl : 100;
    if ($MODE eq 'order') {    # orders exist for ERG pairs only
        my $erg = ERG;
        $s{from} = $erg unless defined $s{from} && ($s{from} eq $erg || $reg->{pair}{ $s{from} }{$erg});
        if    ($s{from} ne $erg)                                       { $s{to} = $erg }
        elsif (defined $s{to} && !$reg->{pair}{$erg}{ $s{to} })        { delete $s{to} }
        delete $s{amount} unless defined $v->{ 'amount_' . $reg->{tokens}{ $s{from} }{decimals} };
    }
    return \%s;
}

# ============================================================ Mosaik views ==

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

sub token_dropdown {
    my ($id, $reg, $ids, $value, $placeholder) = @_;
    return O(type => 'DropDownList', id => $id, value => $value, onValueChanged => 'refresh',
             placeholder => $placeholder, entries => O(map { $_ => $reg->{tokens}{$_}{label} } @$ids));
}

sub quote_placeholder {    # what the quote area shows before / after a preview
    my ($text) = @_;
    return O(type => 'Box', id => 'quote', children => [ button($text || ($MODE eq 'order' ? 'Preview order' : 'Preview swap'), 'preview') ]);
}

sub orders_placeholder {    # collapsed "open orders" area (needs an address)
    my ($address) = @_;
    return O(type => 'Box', id => 'orders',
             children => [ $address ? button('My open orders', 'orders', style => 'TEXT') : () ]);
}

sub title_block {
    return (
        label('SigmaSwap', 'HEADLINE1', color => 'PRIMARY'),
        label('Swap ERG and tokens on the ErgoDex liquidity pools', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
    );
}

# ---- home: the two ways to swap, each explained

sub home_view {
    my ($reg) = @_;
    my @kids = title_block();
    if (!@{ $reg->{order} }) {
        push @kids, label('The pool list is unavailable right now. Please try again in a moment.', 'BODY1', talign => 'CENTER'),
                    button('Retry', 'reload');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    my $order_fee = fmt_amount(cfg('ORDER_MIN_EX_FEE', 6000000), 9);

    push @kids, child(card(column([
        child(label('Direct swap', 'HEADLINE2'), align => 'START'),
        child(label('One transaction between your wallet and the pool. You receive exactly the amount you are shown, '
                    . 'with no extra fee, and nobody can trade ahead of you.', 'BODY1'), align => 'START'),
        child(label('If someone else trades in the same pool at the same moment, your swap does not happen. '
                    . 'Nothing is spent, and you simply try again.', 'BODY2', color => 'SECONDARY'), align => 'START'),
        button('Direct swap', 'goDirect'),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    push @kids, child(card(column([
        child(label('Order', 'HEADLINE2'), align => 'START'),
        child(label('Your funds go into an ErgoDex order and a bot completes the swap for you, even when the pool '
                    . "is busy. Costs about $order_fee ERG extra. Pairs with ERG only. "
                    . 'You can cancel while it is waiting.', 'BODY1'), align => 'START'),
        child(row([
            O(type => 'Icon', icon => 'WARN', size => 'SMALL', tintColor => 'PRIMARY'),
            child(label('Warning: you can be front-run. A waiting order is public, so other traders can trade '
                        . 'ahead of it and you may receive only your minimum amount instead of the quoted one.',
                        'BODY2BOLD', color => 'PRIMARY'), weight => N(1)),
        ], spacing => 'HALF_DEFAULT'), align => 'JUSTIFY'),
        button('Place an order', 'goOrder', style => 'SECONDARY'),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    push @kids, child(card(column([
        child(O(type => 'CheckboxLabel', id => 'simple', text => 'Simple mode', style => 'BODY1BOLD',
                value => ($SIMPLE ? TRUE : FALSE)), align => 'START'),
        child(label('Shows only what you need: what you pay, what you receive, and the button. '
                    . 'Switch it off to see rate, price impact, fees and slippage settings.',
                    'BODY2', color => 'SECONDARY'), align => 'START'),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    push @kids, label('Your wallet signs every transaction; SigmaSwap never holds your funds.',
                      'BODY2', color => 'SECONDARY', talign => 'CENTER');
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

# ---- the swap form of one mode

sub main_view {
    my ($reg, $s, $balance, $quote_box) = @_;
    my $tok   = $reg->{tokens};
    my $from  = defined $s->{from} ? $tok->{ $s->{from} } : undef;
    my $order = $MODE eq 'order';

    my @kids = title_block();
    if (!@{ $reg->{order} }) {
        push @kids, label('The pool list is unavailable right now. Please try again in a moment.', 'BODY1', talign => 'CENTER'),
                    button('Retry', 'reload');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    push @kids, child(row([
        child(label($order ? 'Order' : 'Direct swap', 'HEADLINE2'), weight => N(1)),
        button('Change', 'home', style => 'TEXT'),
    ]), align => 'JUSTIFY');
    push @kids, child(row([
        O(type => 'Icon', icon => 'WARN', size => 'SMALL', tintColor => 'PRIMARY'),
        child(label('A waiting order can be front-run: you may receive only your minimum amount.',
                    'BODY2', color => 'PRIMARY'), weight => N(1)),
    ], spacing => 'HALF_DEFAULT'), align => 'JUSTIFY') if $order;

    # ---- wallet
    push @kids, child(card(column([
        child(label('Your wallet address', 'BODY1BOLD'), align => 'START'),
        O(type => 'ErgoAddressChooseButton', id => 'address', value => $s->{address}, onValueChanged => 'refresh',
          style => $s->{address} ? 'BUTTON_SECONDARY' : undef),
        $SIMPLE ? () : child(label('Funds are taken from, and returned to, this address.', 'BODY2', color => 'SECONDARY'), align => 'START'),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    # ---- pay / receive (orders exist for ERG pairs only)
    my $erg      = ERG;
    my @from_ids = $order ? (grep { $_ eq $erg || $reg->{pair}{$_}{$erg} } @{ $reg->{order} }) : @{ $reg->{order} };
    my @to_ids   = !$from ? ()
                 : ($order && $from->{id} ne $erg) ? ($erg)
                 : (sort { $reg->{pair}{ $from->{id} }{$b}{tvl} <=> $reg->{pair}{ $from->{id} }{$a}{tvl} || $a cmp $b }
                         keys %{ $reg->{pair}{ $from->{id} } });
    my %slip = SLIPPAGE;
    my @form = (
        child(label('You pay', 'BODY1BOLD'), align => 'START'),
        child(token_dropdown('from', $reg, \@from_ids, $s->{from}, 'Select token'), align => 'JUSTIFY'),
    );
    if ($from) {
        push @form, child(O(
            type => 'DecimalInputField', id => "amount_$from->{decimals}",
            value => defined $s->{amount} ? N($s->{amount}) : undef,
            placeholder => "Amount of $from->{ticker}",
            onValueChanged => 'clearQuote', onImeAction => 'preview', minValue => N(1),
            imeActionType => 'DONE', scale => N($from->{decimals}),
        ), align => 'JUSTIFY');
        push @form, child(label('Balance: ' . fmt_amount($balance, $from->{decimals}) . " $from->{ticker}",
                                'BODY2', color => 'SECONDARY'), align => 'END') if defined $balance;
    }
    push @form,
        button("\x{21C5}  Switch", 'flip', style => 'TEXT', enabled => (defined $s->{to} ? 1 : 0)),
        child(label('You receive', 'BODY1BOLD'), align => 'START'),
        child(token_dropdown('to', $reg, \@to_ids, $s->{to}, 'Select token'), align => 'JUSTIFY');
    # simple mode has no slippage control: the 1 % default applies
    push @form,
        child(label($order ? 'Slippage tolerance (sets your minimum)' : 'Slippage tolerance', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DropDownList', id => 'slippage', value => "$s->{slippage}", onValueChanged => 'clearQuote',
                entries => O(map { $_ => $slip{$_} } sort { $a <=> $b } keys %slip)), align => 'JUSTIFY')
        unless $SIMPLE;
    push @kids, child(card(column(\@form, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    # ---- quote / status area, open orders, footer
    push @kids, child($quote_box || quote_placeholder(), align => 'JUSTIFY');
    push @kids, child(orders_placeholder($s->{address}), align => 'JUSTIFY') if $order;
    push @kids, label('Network fee ' . fmt_amount(cfg('MINER_FEE', 1100000), 9) . ' ERG. No service fee. '
                      . 'Your wallet signs every transaction; SigmaSwap never holds your funds.',
                      'BODY2', color => 'SECONDARY', talign => 'CENTER') unless $SIMPLE;

    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

# Every response re-declares the actions with the current mode in their URLs: the
# mode is not an input field, it travels with the buttons.
sub base_actions {
    my $m = "&mode=$MODE&simple=$SIMPLE";
    return (
        # from the start screen: no simple= in the URL, the switch itself is posted
        O(type => 'BackendRequestAction', url => "$API?action=refresh&mode=direct", id => 'goDirect', postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=refresh&mode=order",  id => 'goOrder',  postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=home&simple=$SIMPLE", id => 'home',     postValues => 'NONE'),
        O(type => 'BackendRequestAction', url => "$API?action=refresh$m", id => 'refresh', postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=flip$m",    id => 'flip',    postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=quote$m",   id => 'preview', postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=orders$m",  id => 'orders',  postValues => 'VALID'),
        O(type => 'ReloadAction', id => 'reload'),
        # local (no network): typing a new amount drops a quote that no longer matches it
        O(type => 'ChangeSiteAction', id => 'clearQuote', newContent => O(view => quote_placeholder())),
    );
}

sub mosaik_action { out_json(O(appVersion => N(APP_VERSION), action => $_[0])) }
sub mosaik_dialog {
    my ($msg) = @_;
    mosaik_action(O(type => 'DialogAction', id => 'msg', message => $msg, positiveButtonText => 'OK'));
}
sub mosaik_replace {    # ChangeSiteAction: the element with the same id is replaced in place
    my ($view, @extra_actions) = @_;
    mosaik_action(O(type => 'ChangeSiteAction', id => 'show',
                    newContent => O(actions => [ base_actions(), @extra_actions ], view => $view)));
}

# ================================================================= actions ==

sub act_app {
    my $reg = registry();
    out_json(O(
        manifest => O(
            appName               => 'SigmaSwap',
            appDescription        => 'Swap ERG and Ergo tokens on the ErgoDex liquidity pools.',
            iconUrl               => "$BASE/icon.png",
            appVersion            => N(APP_VERSION),
            targetMosaikVersion   => N(2),
            targetCanvasDimension => 'COMPACT_WIDTH',
            cacheLifetime         => N(3600),
        ),
        actions => [ base_actions() ],
        view    => home_view($reg),
    ));
}

sub act_home { mosaik_replace(home_view(registry())) }

sub lookup_balance {
    my ($s) = @_;
    return undef unless $s->{address} && $s->{from};
    my $r = chain('balance', { address => $s->{address}, token => $s->{from} });
    return $r->{ok} ? $r->{balance} : undef;
}

sub act_refresh {
    my $reg = registry();
    my $s   = clean_state(posted_values(), $reg);
    mosaik_replace(main_view($reg, $s, lookup_balance($s)));
}

sub act_flip {
    my $reg = registry();
    my $s   = clean_state(posted_values(), $reg);
    if (defined $s->{from} && defined $s->{to}) {
        @$s{qw(from to)} = @$s{qw(to from)};
        delete $s->{amount};    # the amount was in units of the other token
    }
    mosaik_replace(main_view($reg, $s, lookup_balance($s)));
}

# ---- status of a submitted swap/order
# The wallet tells us nothing after signing, so each quote carries a random id.
# When the wallet fetches the transaction we note its id under that random id;
# afterwards the status screen looks the transaction up on the chain.

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

sub status_box {
    my ($title, $lines, $buttons) = @_;
    my @rows = (child(label($title, 'BODY1BOLD'), align => 'START'));
    push @rows, map { child(ref $_ ? $_ : label($_, 'BODY1'), align => 'START') } @$lines;
    push @rows, @$buttons;
    return O(type => 'Box', id => 'quote',
             children => [ child(card(column(\@rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]);
}

sub act_status {
    my $nonce = $cgi->url_param('n');
    return mosaik_dialog('Unknown swap.') unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/;
    my $reg    = registry();
    my $self   = O(type => 'BackendRequestAction', url => "$API?action=status&mode=$MODE&simple=$SIMPLE&n=$nonce", id => 'status', postValues => 'NONE');
    my $again  = button('Refresh status', 'status', style => 'SECONDARY');
    my $new    = button('Back to the form', 'clearQuote', style => 'TEXT');
    my $what   = $MODE eq 'order' ? 'order' : 'swap';

    my $rec = load_record($nonce);
    if (!$rec) {
        return mosaik_replace(status_box("Waiting for your wallet", [
            "Your wallet has not picked up this $what yet. If you cancelled signing, nothing happened.",
        ], [ $again, $new ]), $self);
    }
    my $r = chain('status', { map { $_ => $rec->{$_} } qw(tx_id watch_box order_box mode address to) });
    return mosaik_dialog($r->{error} || 'Could not check the status. Please try again.') unless $r->{ok};

    my $summary = describe_amount($reg, $rec->{from}, $rec->{amount}) . " \x{2192} " . ($rec->{to} =~ /^[0-9a-f]{64}\z/ ? ($reg->{tokens}{ $rec->{to} }{ticker} // 'token') : 'token');
    my $web = cfg('EXPLORER_WEB_URL', 'https://explorer.ergoplatform.com/en/transactions/');
    my @extra = ($self, O(type => 'BrowserAction', url => $web . $rec->{tx_id}, id => 'viewTx'));
    my $view  = button('View transaction', 'viewTx', style => 'TEXT');
    my $age   = time - ($rec->{t} || time);
    my $state = $r->{state} // 'unknown';
    my ($title, @lines, @buttons);

    if ($state eq 'confirmed') {
        $title   = 'Swap complete';
        @lines   = ($summary, 'You received ' . describe_amount($reg, $rec->{to}, $rec->{out}) . '.');
        @buttons = ($view, $new);
    }
    elsif ($state eq 'executed') {
        $title   = 'Order executed';
        @lines   = ($summary, defined $r->{received} ? 'You received ' . describe_amount($reg, $rec->{to}, $r->{received}) . '.'
                                                     : 'The bot completed your order.');
        @buttons = ($view, $new);
    }
    elsif ($state eq 'order_open' || $state eq 'executing') {
        $title   = $state eq 'executing' ? 'Order is being executed' : 'Order is waiting for a bot';
        @lines   = ($summary, 'This usually takes a few minutes. If the price is below your minimum it waits; '
                              . 'you can cancel it under "My open orders".');
        @buttons = ($again, $view, $new);
    }
    elsif ($state eq 'cancelled') {
        $title   = 'Order cancelled';
        @lines   = ($summary, 'The order was refunded to your address.');
        @buttons = ($view, $new);
    }
    elsif ($state eq 'pending') {
        $title   = $MODE eq 'order' ? 'Order submitted' : 'Swap submitted';
        @lines   = ($summary, 'Waiting for the next block (about two minutes).');
        @buttons = ($again, $view, $new);
    }
    elsif ($state eq 'failed') {
        $title   = $MODE eq 'order' ? 'Order was not placed' : 'Swap did not go through';
        @lines   = ($summary, $MODE eq 'order'
                        ? 'The funds it needed were spent by another transaction first. Nothing was lost. Preview again.'
                        : 'Someone else traded in this pool first. Nothing was spent. Preview again for a fresh price.');
        @buttons = (button('Preview again', 'preview'), $new);
    }
    elsif ($age < 180) {
        $title   = 'Waiting for the network';
        @lines   = ($summary, 'Your transaction has not shown up yet. This can take a moment after signing. '
                              . 'If you cancelled signing, nothing happened.');
        @buttons = ($again, $new);
    }
    else {
        $title   = 'Not found on the network';
        @lines   = ($summary, 'Your transaction never appeared. If you cancelled signing, nothing happened. '
                              . "Otherwise check your wallet's history before trying again.");
        @buttons = ($again, $new);
    }
    mosaik_replace(status_box($title, \@lines, \@buttons), @extra);
}

sub act_quote {
    my $reg = registry();
    my $s   = clean_state(posted_values(), $reg);
    return mosaik_dialog('Choose the token you want to pay with.')   unless defined $s->{from};
    return mosaik_dialog('Choose the token you want to receive.')    unless defined $s->{to};
    return mosaik_dialog('Enter the amount you want to swap.')       unless defined $s->{amount};

    my $pool = $reg->{pair}{ $s->{from} }{ $s->{to} };
    my ($from, $to) = @{ $reg->{tokens} }{ $s->{from}, $s->{to} };
    my $q = chain('quote', { pool => $pool->{id}, from => $s->{from}, amount => $s->{amount}, address => $s->{address} });
    return mosaik_dialog($q->{error} || 'Could not get a quote. Please try again.') unless $q->{ok};
    return mosaik_dialog('This pool no longer matches the selected pair. Please reopen the app.')
        unless $q->{to} eq $s->{to} && "$q->{out}" =~ /^\d+\z/;
    return mosaik_dialog('This amount is too small to swap in this pool.') if $q->{out} eq '0';

    my $min_out = Math::BigInt->new($q->{out})->bmul(10000 - $s->{slippage})->bdiv(10000);
    $min_out = Math::BigInt->new(1) if $min_out->is_zero;
    my $fee   = cfg('MINER_FEE', 1100000) + 0;
    my $bps   = $q->{impact_bps} + 0;

    # ---- order mode: the same swap, executed by an ErgoDex bot (ERG pairs only)
    my $order = ($MODE eq 'order' && ref $q->{order} eq 'HASH') ? $q->{order} : undef;
    return mosaik_dialog('Orders are only available for pairs with ERG. Go back and choose "Direct swap" for this pair.')
        if $MODE eq 'order' && !$order;
    my ($show_out, $show_min, $ex_fee) = ($q->{out}, $min_out->bstr, undef);
    if ($order) {
        # the bot's fee grows with the output: min_ex_fee at the minimum output
        $ex_fee = Math::BigInt->new($order->{min_ex_fee})->bmul($q->{out})->bdiv($min_out);
        $ex_fee = Math::BigInt->new($order->{max_ex_fee}) if $ex_fee->bcmp($order->{max_ex_fee}) > 0;
        if ($s->{to} eq ERG) {    # fee comes out of the ERG received
            return mosaik_dialog('This amount is too small for an order: the execution fee would eat it. '
                                 . 'Choose "Direct swap" instead.')
                if $min_out->bcmp(Math::BigInt->new($order->{max_ex_fee})->bmul(3)) < 0;
            $show_out = Math::BigInt->new($q->{out})->bsub($ex_fee)->bstr;
            $show_min = $min_out->copy->bsub($order->{min_ex_fee})->bstr;
        }
    }

    my $pay  = fmt_amount($s->{amount}, $from->{decimals}) . " $from->{ticker}";
    my $get  = fmt_amount($show_out,    $to->{decimals})   . " $to->{ticker}";
    my $rate = fmt_rate($s->{amount}, $from->{decimals}, $q->{out}, $to->{decimals});

    # the received amount as a TokenLabel: the wallet adds its own verified/unverified
    # badge and opens the token details on tap
    my $get_el = $s->{to} eq ERG
        ? label($get, 'HEADLINE2', color => 'PRIMARY', talign => 'END')
        : O(type => 'TokenLabel', tokenId => $s->{to}, tokenName => $to->{ticker},
            decimals => $to->{decimals} ? N($to->{decimals}) : undef, amount => N($show_out),
            style => 'HEADLINE2', textColor => 'PRIMARY');

    my @rows = (
        # the two token logos, pay -> receive
        O(type => 'Row', packed => TRUE, spacing => 'DEFAULT', children => [
            O(type => 'Image', url => icon_url($s->{from}), size => 'SMALL'),
            O(type => 'Icon', icon => 'FORWARD', size => 'MEDIUM', tintColor => 'SECONDARY'),
            O(type => 'Image', url => icon_url($s->{to}), size => 'SMALL'),
        ]),
        kv_row('You pay', $pay),
        kv_row($order ? 'You receive (estimate)' : 'You receive', $get_el),
    );
    if ($SIMPLE) {
        # the essentials only; an order still shows the two things that differ from the estimate
        push @rows, kv_row('At least', fmt_amount($show_min, $to->{decimals}) . " $to->{ticker}"),
                    kv_row('Bot fee', "\x{2248} " . fmt_amount($ex_fee->bstr, 9, 4) . ' ERG') if $order;
    }
    else {
        push @rows, detail_rows($q, $s, $from, $to, $rate, $bps, $fee, $order, $ex_fee, $show_min);
    }
    push @rows, child(label("Warning: this swap moves the pool price by " . fmt_pct($bps)
                            . ". You will receive noticeably less than the market rate.", 'BODY2BOLD', color => 'PRIMARY'),
                      align => 'START') if $bps >= 500;
    return finish_quote($q, $s, $from, $to, $pool, $min_out, $fee, $order, \@rows);
}

# Token logos are bundled PNGs (icons/<token id>.png, built by tools/fetch_icons.py);
# a token without one gets the generic placeholder.
sub icon_path {
    my ($id) = @_;
    return ($id =~ /^[0-9a-f]{64}\z/ && -f "$FindBin::Bin/icons/$id.png") ? "icons/$id.png" : 'icons/unknown.png';
}
sub icon_url { return "$BASE/" . icon_path($_[0]) }

sub detail_rows {
    my ($q, $s, $from, $to, $rate, $bps, $fee, $order, $ex_fee, $show_min) = @_;
    my @rows = (
        O(type => 'HorizontalRule', vPadding => 'HALF_DEFAULT'),
        kv_row('Rate', "1 $from->{ticker} \x{2248} $rate $to->{ticker}"),
        kv_row('Price impact', fmt_pct($bps)),
        kv_row('Pool fee', fmt_pct(($q->{fee_denom} - $q->{fee_num}) * 10000 / $q->{fee_denom})),
    );
    push @rows, kv_row('Execution fee', "\x{2248} " . fmt_amount($ex_fee->bstr, 9, 4) . ' ERG (max '
                                        . fmt_amount($order->{max_ex_fee}, 9) . ')') if $order;
    push @rows,
        kv_row('Network fee', fmt_amount($fee, 9) . ' ERG'),
        kv_row('Minimum received', fmt_amount($show_min, $to->{decimals}) . " $to->{ticker}");
    push @rows, child(label('An ErgoDex bot executes the order at the pool price at that moment, usually within a '
                            . 'few minutes, and never below the minimum. '
                            . ($s->{to} eq ERG ? 'The execution fee is taken from the ERG you receive. '
                                               : 'The execution fee is paid in ERG on top; the unused part comes back. ')
                            . 'Until it is executed you can cancel it under "My open orders".',
                            'BODY2', color => 'SECONDARY'), align => 'START') if $order;
    push @rows, child(label('Priced on top of ' . ($q->{pending} == 1 ? '1 swap' : "$q->{pending} swaps")
                            . ' already waiting in this pool.', 'BODY2', color => 'SECONDARY'), align => 'START')
        if !$order && ($q->{pending} || 0) > 0;
    return @rows;
}

sub finish_quote {    # balance check, Swap / Place order button, and the response
    my ($q, $s, $from, $to, $pool, $min_out, $fee, $order, $rows) = @_;
    my @rows = @$rows;
    my @extra;
    my $short = 0;
    if (defined $q->{balance}) {
        my $need = Math::BigInt->new($s->{amount});
        if ($s->{from} eq ERG) {
            $need->badd($fee + 1000000);                  # network fee + a minimal change box
            $need->badd($order->{extra}) if $order;       # execution fee deposit
        }
        $short = 1 if $need->bcmp(Math::BigInt->new($q->{balance})) > 0;
    }
    if (!$s->{address}) {
        push @rows, label('Choose your wallet address above to swap.', 'BODY1', talign => 'CENTER'),
                    button('Refresh quote', 'preview', style => 'SECONDARY');
    }
    elsif ($short) {
        my $have = fmt_amount($q->{balance}, $from->{decimals}) . " $from->{ticker}";
        push @rows, label($s->{from} eq ERG
                              ? "Not enough ERG: this address holds $have, and the swap also needs the fees."
                              : "Not enough $from->{ticker}: this address holds $have.", 'BODY1', talign => 'CENTER'),
                    button('Refresh quote', 'preview', style => 'SECONDARY');
    }
    else {
        my $url = 'ergopay://' . ergopay_host() . '/api.pl?action=ergopay'
                . "&p=$pool->{id}&f=$s->{from}&a=$s->{amount}&m=" . $min_out->bstr . "&addr=$s->{address}"
                . ($order ? '&o=1' : '');
        # a random id links this quote to the transaction the wallet is about to fetch (see act_status)
        my $nonce = new_nonce();
        if (defined $nonce && record_file($nonce)) {
            $url .= "&n=$nonce";
            push @extra, O(type => 'BackendRequestAction', url => "$API?action=status&mode=$MODE&simple=$SIMPLE&n=$nonce",
                           id => 'status', postValues => 'NONE');
        }
        push @extra, O(type => 'ErgoPayAction', url => $url, id => 'pay',
                       onFinished => (defined $nonce && record_file($nonce)) ? 'status' : 'refresh');
        push @rows, button($order ? 'Place order' : "Swap $from->{ticker} for $to->{ticker}", 'pay'),
                    button('Refresh quote', 'preview', style => 'TEXT');
    }

    my $box = O(type => 'Box', id => 'quote',
                children => [ child(card(column(\@rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]);
    mosaik_replace($box, @extra);
}

# ---- open orders: found on-chain by the address's key, nothing is stored here

sub ergopay_host { my $h = $BASE; $h =~ s{^https?://}{}; return $h }

sub describe_amount {    # "5 ERG" for known tokens, a short id for unknown ones
    my ($reg, $id, $raw) = @_;
    my $t = $reg->{tokens}{$id};
    return $t ? fmt_amount($raw, $t->{decimals}) . " $t->{ticker}"
              : "$raw units of token " . substr($id, 0, 6) . "\x{2026}";
}

sub act_orders {
    my $reg = registry();
    my $s   = clean_state(posted_values(), $reg);
    return mosaik_dialog('Choose your wallet address first.') unless $s->{address};
    my $r = chain('orders', { address => $s->{address} });
    return mosaik_dialog($r->{error} || 'Could not load your orders. Please try again.') unless $r->{ok};

    my (@rows, @extra);
    push @rows, child(label('Open orders', 'BODY1BOLD'), align => 'START');
    my $n = 0;
    for my $o (@{ $r->{orders} || [] }) {
        next unless ($o->{box_id} // '') =~ /^[0-9a-f]{64}\z/ && ($o->{amount} // '') =~ /^\d+\z/
                 && ($o->{min_out} // '') =~ /^\d+\z/ && ($o->{from} // '') =~ /^[0-9a-f]{64}\z/
                 && ($o->{to} // '') =~ /^[0-9a-f]{64}\z/;
        $n++;
        push @extra, O(type => 'ErgoPayAction', id => "refund$n", onFinished => 'orders',
                       url => 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&r=$o->{box_id}&addr=$s->{address}");
        push @rows,
            O(type => 'HorizontalRule', vPadding => 'QUARTER_DEFAULT'),
            child(label('Sell ' . describe_amount($reg, $o->{from}, $o->{amount}) . ' for at least '
                        . describe_amount($reg, $o->{to}, $o->{min_out}), 'BODY1'), align => 'START'),
            button('Cancel and refund', "refund$n", style => 'SECONDARY');
    }
    push @rows, child(label('No open orders on this address. Executed and cancelled orders disappear from this list.',
                            'BODY2', color => 'SECONDARY'), align => 'START') unless $n;
    push @rows, child(label('An order that is still here after a few minutes is waiting for the price to come back '
                            . 'above its minimum. You can cancel it at any time.', 'BODY2', color => 'SECONDARY'),
                      align => 'START') if $n;
    push @rows, button('Refresh', 'orders', style => 'TEXT');
    mosaik_replace(O(type => 'Box', id => 'orders',
                     children => [ child(card(column(\@rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]),
                   @extra);
}

# ---- ErgoPay: the wallet fetches the transaction here, at the moment of signing,
# so it is always built against the pool's latest state.

sub ergopay_error { out_json(O(message => $_[0], messageSeverity => 'ERROR')) }

sub act_ergopay {
    my %p = map { $_ => scalar $cgi->param($_) } qw(p f a m addr o r n);
    return ergopay_refund(\%p) if defined $p{r};
    my $order = (defined $p{o} && $p{o} eq '1') ? 1 : 0;
    for my $k (qw(p f)) {
        return ergopay_error('Invalid swap request.') unless defined $p{$k} && $p{$k} =~ /^[0-9a-f]{64}\z/;
    }
    for my $k (qw(a m)) {
        return ergopay_error('Invalid swap request.') unless defined $p{$k} && $p{$k} =~ /^[1-9]\d{0,18}\z/;
    }
    return ergopay_error('Invalid swap request.')
        unless defined $p{addr} && $p{addr} =~ /^[1-9A-HJ-NP-Za-km-z]{40,60}\z/;

    # only pools from the published list, and only the pair that pool really trades
    my $reg = registry();
    my ($pool) = grep { $_->{id} eq $p{p} } map { values %$_ } values %{ $reg->{pair} };
    return ergopay_error('This pool is not available for swaps.')
        unless $pool && ($pool->{x}{id} eq $p{f} || $pool->{y}{id} eq $p{f});

    my $r = chain('build', { pool => $p{p}, from => $p{f}, amount => $p{a}, min_out => $p{m}, address => $p{addr},
                             mode => $order ? 'order' : 'direct' });
    return ergopay_error($r->{error} || 'The swap could not be prepared. Please try again.') unless $r->{ok};

    my ($from, $to) = @{ $reg->{tokens} }{ $r->{from}, $r->{to} };
    return ergopay_error('The swap could not be prepared. Please try again.') unless $from && $to;
    warn "sigmaswap: built " . ($order ? 'order' : 'swap') . " tx $r->{tx_id} pool " . substr($p{p}, 0, 8) . "\n";    # audit trail, public data only
    save_record($p{n}, {
        t => time, mode => ($order ? 'order' : 'direct'), tx_id => $r->{tx_id}, watch_box => $r->{watch_box},
        order_box => $r->{order_box}, address => $r->{address}, from => $r->{from}, to => $r->{to},
        amount => $r->{amount}, out => $r->{out},
    }) if defined $p{n} && $p{n} =~ /^[0-9a-f]{32}\z/;
    if ($order) {
        return out_json(O(
            reducedTx       => $r->{reduced_tx},
            address         => $r->{address},
            message         => 'SigmaSwap order: sell ' . fmt_amount($r->{amount}, $from->{decimals}) . " $from->{ticker} for about "
                               . fmt_amount($r->{out}, $to->{decimals}) . " $to->{ticker}. An ErgoDex bot executes it; execution fee up to "
                               . fmt_amount($r->{max_ex_fee}, 9) . ' ERG, network fee ' . fmt_amount($r->{miner_fee}, 9)
                               . ' ERG. You can cancel the order in SigmaSwap until it is executed.',
            messageSeverity => 'INFORMATION',
        ));
    }
    out_json(O(
        reducedTx       => $r->{reduced_tx},
        address         => $r->{address},
        message         => 'SigmaSwap: swap ' . fmt_amount($r->{amount}, $from->{decimals}) . " $from->{ticker} for "
                           . fmt_amount($r->{out}, $to->{decimals}) . " $to->{ticker}. Network fee "
                           . fmt_amount($r->{miner_fee}, 9) . ' ERG.',
        messageSeverity => 'INFORMATION',
    ));
}

sub ergopay_refund {
    my ($p) = @_;
    return ergopay_error('Invalid request.')
        unless $p->{r} =~ /^[0-9a-f]{64}\z/ && defined $p->{addr} && $p->{addr} =~ /^[1-9A-HJ-NP-Za-km-z]{40,60}\z/;
    my $r = chain('refund', { box => $p->{r}, address => $p->{addr} });
    return ergopay_error($r->{error} || 'The refund could not be prepared. Please try again.') unless $r->{ok};
    my $reg = registry();
    warn "sigmaswap: built refund tx $r->{tx_id}\n";
    out_json(O(
        reducedTx       => $r->{reduced_tx},
        address         => $r->{address},
        message         => 'SigmaSwap: cancel your order and return ' . describe_amount($reg, $r->{from}, $r->{amount})
                           . ' (plus the unused fee deposit) to your address. Network fee '
                           . fmt_amount($r->{miner_fee}, 9) . ' ERG.',
        messageSeverity => 'INFORMATION',
    ));
}

# ---- landing page helpers

sub act_pools {
    my $reg = registry();
    my (%done, @list);
    for my $p (sort { $b->{tvl} <=> $a->{tvl} } map { values %$_ } values %{ $reg->{pair} }) {
        next if $done{ $p->{id} }++;
        push @list, O(x => $reg->{tokens}{ $p->{x}{id} }{label}, y => $reg->{tokens}{ $p->{y}{id} }{label},
                      xi => icon_path($p->{x}{id}), yi => icon_path($p->{y}{id}),
                      tvl => N(sprintf('%.2f', $p->{tvl})));
    }
    success_json(pools => \@list);
}

sub act_health {
    my $r = chain('health', { network => 1 });
    return die_json($r->{error} || 'helper failed') unless $r->{ok};
    # no version numbers here: this endpoint is public
    success_json(helper => 'ok', height => N($r->{height} || 0), pools => N(scalar @{ get_pools() }), base_url => $BASE);
}
