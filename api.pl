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
#   ErgoPay (EIP-20), fetched by the wallet when the user taps Swap
#     GET  api.pl?action=ergopay&... -> { reducedTx, address, message, messageSeverity }
#   Landing page (index.html / app.js), POST FormData like every other AAP app
#     POST api.pl  action=pools      -> { success: 1, pools: [...] }
#     POST api.pl  action=health     -> { success: 1, ... }   (deploy gate)
#
# The app is public and stateless: no login, no database, no cookies, no keys.
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

use constant APP_VERSION => 1;     # bump when the Mosaik view changes: open wallets reload
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

my %actions = (
    ''        => \&act_app,
    'refresh' => \&act_refresh,
    'flip'    => \&act_flip,
    'quote'   => \&act_quote,
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
        elsif ($action =~ /^(refresh|flip|quote)\z/)  { mosaik_dialog('Something went wrong. Please try again.') }
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

sub cache_file {
    my $dir = cfg('CACHE_DIR', File::Spec->catdir(File::Spec->tmpdir, "sigmaswap-cache-$>"));
    mkdir $dir, 0770 unless -d $dir;
    return (-d $dir && -w $dir) ? File::Spec->catfile($dir, 'pools.json') : undef;
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
    return O(type => 'Box', id => 'quote', children => [ button($text || 'Preview swap', 'preview') ]);
}

sub main_view {
    my ($reg, $s, $balance, $quote_box) = @_;
    my $tok  = $reg->{tokens};
    my $from = defined $s->{from} ? $tok->{ $s->{from} } : undef;

    my @kids = (
        label('SigmaSwap', 'HEADLINE1', color => 'PRIMARY'),
        label('Swap tokens directly against the ErgoDex liquidity pools', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
    );

    if (!@{ $reg->{order} }) {
        push @kids, label('The pool list is unavailable right now. Please try again in a moment.', 'BODY1', talign => 'CENTER'),
                    button('Retry', 'reload');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }

    # ---- wallet
    push @kids, child(card(column([
        child(label('Your wallet address', 'BODY1BOLD'), align => 'START'),
        O(type => 'ErgoAddressChooseButton', id => 'address', value => $s->{address}, onValueChanged => 'refresh',
          style => $s->{address} ? 'BUTTON_SECONDARY' : undef),
        child(label('Funds are taken from, and returned to, this address.', 'BODY2', color => 'SECONDARY'), align => 'START'),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    # ---- pay / receive
    my @to_ids = $from ? (sort { $reg->{pair}{ $from->{id} }{$b}{tvl} <=> $reg->{pair}{ $from->{id} }{$a}{tvl} || $a cmp $b }
                               keys %{ $reg->{pair}{ $from->{id} } }) : ();
    my %slip = SLIPPAGE;
    my @form = (
        child(label('You pay', 'BODY1BOLD'), align => 'START'),
        child(token_dropdown('from', $reg, $reg->{order}, $s->{from}, 'Select token'), align => 'JUSTIFY'),
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
        child(token_dropdown('to', $reg, \@to_ids, $s->{to}, 'Select token'), align => 'JUSTIFY'),
        child(label('Slippage tolerance', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DropDownList', id => 'slippage', value => "$s->{slippage}", onValueChanged => 'clearQuote',
                entries => O(map { $_ => $slip{$_} } sort { $a <=> $b } keys %slip)), align => 'JUSTIFY');
    push @kids, child(card(column(\@form, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    # ---- quote area + footer
    push @kids, child($quote_box || quote_placeholder(), align => 'JUSTIFY');
    push @kids, label('Network fee ' . fmt_amount(cfg('MINER_FEE', 1100000), 9) . ' ERG. No service fee. '
                      . 'Your wallet signs every swap; SigmaSwap never holds your funds.',
                      'BODY2', color => 'SECONDARY', talign => 'CENTER');

    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub base_actions {
    return (
        O(type => 'BackendRequestAction', url => "$API?action=refresh", id => 'refresh', postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=flip",    id => 'flip',    postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=quote",   id => 'preview', postValues => 'VALID'),
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
    my $s   = clean_state({}, $reg);
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
        view    => main_view($reg, $s),
    ));
}

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
    my $pay   = fmt_amount($s->{amount}, $from->{decimals}) . " $from->{ticker}";
    my $get   = fmt_amount($q->{out},    $to->{decimals})   . " $to->{ticker}";
    my $rate  = fmt_rate($s->{amount}, $from->{decimals}, $q->{out}, $to->{decimals});
    my $bps   = $q->{impact_bps} + 0;

    # the received amount as a TokenLabel: the wallet adds its own verified/unverified
    # badge and opens the token details on tap
    my $get_el = $s->{to} eq ERG
        ? label($get, 'HEADLINE2', color => 'PRIMARY', talign => 'END')
        : O(type => 'TokenLabel', tokenId => $s->{to}, tokenName => $to->{ticker},
            decimals => $to->{decimals} ? N($to->{decimals}) : undef, amount => N($q->{out}),
            style => 'HEADLINE2', textColor => 'PRIMARY');

    my @rows = (
        kv_row('You pay', $pay),
        kv_row('You receive', $get_el),
        O(type => 'HorizontalRule', vPadding => 'HALF_DEFAULT'),
        kv_row('Rate', "1 $from->{ticker} \x{2248} $rate $to->{ticker}"),
        kv_row('Price impact', fmt_pct($bps)),
        kv_row('Pool fee', fmt_pct(($q->{fee_denom} - $q->{fee_num}) * 10000 / $q->{fee_denom})),
        kv_row('Network fee', fmt_amount($fee, 9) . ' ERG'),
        kv_row('Minimum received', fmt_amount($min_out->bstr, $to->{decimals}) . " $to->{ticker}"),
    );
    push @rows, child(label("Warning: this swap moves the pool price by " . fmt_pct($bps)
                            . ". You will receive noticeably less than the market rate.", 'BODY2BOLD', color => 'PRIMARY'),
                      align => 'START') if $bps >= 500;

    my @extra;
    my $short = 0;
    if (defined $q->{balance}) {
        my $need = Math::BigInt->new($s->{amount});
        $need->badd($fee + 1000000) if $s->{from} eq ERG;      # fee + a minimal change box
        $short = 1 if $need->bcmp(Math::BigInt->new($q->{balance})) > 0;
    }
    if (!$s->{address}) {
        push @rows, label('Choose your wallet address above to swap.', 'BODY1', talign => 'CENTER'),
                    button('Refresh quote', 'preview', style => 'SECONDARY');
    }
    elsif ($short) {
        my $have = fmt_amount($q->{balance}, $from->{decimals}) . " $from->{ticker}";
        push @rows, label($s->{from} eq ERG
                              ? "Not enough ERG: this address holds $have, and the swap also needs the network fee."
                              : "Not enough $from->{ticker}: this address holds $have.", 'BODY1', talign => 'CENTER'),
                    button('Refresh quote', 'preview', style => 'SECONDARY');
    }
    else {
        my $host = $BASE;
        $host =~ s{^https?://}{};
        my $url = "ergopay://$host/api.pl?action=ergopay"
                . "&p=$pool->{id}&f=$s->{from}&a=$s->{amount}&m=" . $min_out->bstr . "&addr=$s->{address}";
        push @extra, O(type => 'ErgoPayAction', url => $url, id => 'pay', onFinished => 'refresh');
        push @rows, button("Swap $from->{ticker} for $to->{ticker}", 'pay'),
                    button('Refresh quote', 'preview', style => 'TEXT');
    }

    my $box = O(type => 'Box', id => 'quote',
                children => [ child(card(column(\@rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]);
    mosaik_replace($box, @extra);
}

# ---- ErgoPay: the wallet fetches the transaction here, at the moment of signing,
# so it is always built against the pool's latest state.

sub ergopay_error { out_json(O(message => $_[0], messageSeverity => 'ERROR')) }

sub act_ergopay {
    my %p = map { $_ => scalar $cgi->param($_) } qw(p f a m addr);
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

    my $r = chain('build', { pool => $p{p}, from => $p{f}, amount => $p{a}, min_out => $p{m}, address => $p{addr} });
    return ergopay_error($r->{error} || 'The swap could not be prepared. Please try again.') unless $r->{ok};

    my ($from, $to) = @{ $reg->{tokens} }{ $r->{from}, $r->{to} };
    return ergopay_error('The swap could not be prepared. Please try again.') unless $from && $to;
    warn "sigmaswap: built swap tx $r->{tx_id} pool " . substr($p{p}, 0, 8) . "\n";    # audit trail, public data only
    out_json(O(
        reducedTx       => $r->{reduced_tx},
        address         => $r->{address},
        message         => 'SigmaSwap: swap ' . fmt_amount($r->{amount}, $from->{decimals}) . " $from->{ticker} for "
                           . fmt_amount($r->{out}, $to->{decimals}) . " $to->{ticker}. Network fee "
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
