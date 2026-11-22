#!/usr/bin/perl
# SigmaBridge - two ways out of Ergo: the Rosen bridge (tokens stay themselves on another
# chain) and Swapuz (a custodial exchange sells ERG for another coin; see "Swapuz" below).
# A subdirectory app of SigmaSwap (shared config, helper, venv, cache):
#   https://<host>/cgi-bin/sigmaswap/sigmabridge/
#
#   Mosaik
#     GET  api.pl                  -> start screen: wallet address, then Rosen bridge or Swapuz
#     POST api.pl?action=home      -> the start screen again (keeps the address)
#     POST api.pl?action=sz / szpreview / szstatus&n=   -> the Swapuz form, its preview, its status
#     POST api.pl?action=form      -> the Rosen form, re-rendered (token / chain changed)
#     POST api.pl?action=preview   -> fees, what arrives, the target address to check, Bridge button
#     POST api.pl?action=status&n= -> did the transfer leave Ergo       GET ?action=notify
#   ErgoPay
#     GET  api.pl?action=ergopay&n=<id>&addr=<address>
#
# Out of Ergo only, to every chain the Rosen app offers. A transfer is one transaction that locks the amount at Rosen's
# lock address with the target chain and address attached; Rosen delivers it on
# the other chain. A wrong target address or too low a fee is not refunded, so the
# helper reads the fees from Rosen's on-chain table and checksums the address.
# Public, no login, no keys.

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/..";
use CGI ();
use JSON::PP ();
use Math::BigInt;
use Math::BigFloat;
use File::Spec;
use AppConfig qw(cfg base_url chain);
use MosaikKit;

use constant APP_VERSION => 7;
use constant CHAINS      => (cardano => 'Cardano', ethereum => 'Ethereum', binance => 'BNB Chain', 'bitcoin-runes' => 'Bitcoin Runes',
                            bitcoin => 'Bitcoin', doge => 'Dogecoin', firo => 'Firo');
use constant CHAIN_ORDER => qw(cardano ethereum binance bitcoin-runes bitcoin doge firo);    # same order as the Rosen app

$CGI::POST_MAX        = 64 * 1024;
$CGI::DISABLE_UPLOADS = 1;

my $cgi = CGI->new;
print $cgi->header(-type => 'application/json', -charset => 'utf-8', -cache_control => 'no-store');

my $BASE = base_url('sigmabridge');
my $API  = "$BASE/api.pl";

my $action = $cgi->url_param('action');
$action = $cgi->param('action') unless defined $action && $action ne '';
$action = '' unless defined $action;

my %mosaik = ('' => \&act_app, home => \&act_home, form => \&act_form, preview => \&act_preview, status => \&act_status,
              sz => \&act_sz, szpreview => \&act_szpreview, szstatus => \&act_szstatus);

if ($cgi->cgi_error) { die_json('Bad request') }
elsif ($action eq 'ergopay') {
    eval { act_ergopay(); 1 } or do { warn "sigmabridge: ergopay failed: $@"; ergopay_error('Something went wrong. Please try again.') };
}
elsif ($action eq 'notify') {
    eval { act_notify(); 1 } or do { warn "sigmabridge: notify failed: $@"; notify_json(undef, 0, 240) };
}
elsif (exists $mosaik{$action}) {
    eval { $mosaik{$action}->(); 1 } or do { warn "sigmabridge: '$action' failed: $@"; mosaik_dialog('Something went wrong. Please try again.') };
}
else { die_json('Unknown action') }
exit;

# ================================================================= helpers ==

sub posted_values {
    my $raw = $cgi->param('POSTDATA');
    return {} unless defined $raw && length $raw;
    my $v = eval { JSON::PP->new->utf8->decode($raw) };
    return ref $v eq 'HASH' ? $v : {};
}
sub good_address { my ($a) = @_; return (defined $a && !ref $a && $a =~ /^[1-9A-HJ-NP-Za-km-z]{40,60}\z/) ? $a : undef }
sub ergopay_host { my $h = $BASE; $h =~ s{^https?://}{}; return $h }
sub amt          { my ($raw, $t) = @_; return fmt_amount($raw, $t->{decimals}) . " $t->{name}" }

sub tokens {    # bundled list (built from Rosen's token map by tools/fetch_bridge_tokens.py)
    open(my $fh, '<', "$FindBin::Bin/tokens.json") or return [];
    local $/;
    my $d = eval { JSON::PP->new->utf8->decode(scalar <$fh>) };
    close $fh;
    my %ok = CHAINS;
    return [ grep { ref $_ eq 'HASH' && ($_->{id} // '') =~ /^(erg|[0-9a-f]{64})\z/ && ref $_->{chains} eq 'ARRAY'
                    && ($_->{decimals} // -1) =~ /^\d{1,2}\z/ && grep { $ok{$_} } @{ $_->{chains} } } @{ ref $d eq 'ARRAY' ? $d : [] } ];
}

sub state {    # the form's values, reduced to known-good ones. Order as in the Rosen app: chain, then token.
    my ($v) = @_;
    my %s = (address => good_address($v->{address}));
    my $list = tokens();
    my %offered;                                   # chain => tokens that can go there
    for my $t (@$list) { push @{ $offered{$_} }, $t for @{ $t->{chains} } }
    $s{chains} = [ grep { $offered{$_} } CHAIN_ORDER ];
    ($s{chain}) = grep { defined $v->{chain} && !ref $v->{chain} && $_ eq $v->{chain} } @{ $s{chains} };
    $s{list} = $s{chain} ? $offered{ $s{chain} } : [];
    ($s{token}) = grep { defined $v->{token} && !ref $v->{token} && $_->{id} eq $v->{token} } @{ $s{list} };
    if ($s{token}) {
        my $a = $v->{ "amount_$s{token}{decimals}" };
        $s{amount} = "$a" if defined $a && !ref $a && "$a" =~ /^[1-9]\d{0,18}\z/;
    }
    my $to = $v->{to};
    if (defined $to && !ref $to) { $to =~ s/\s+//g; $s{to} = $to if $to =~ /^[A-Za-z0-9]{20,120}\z/ }
    return \%s;
}

# ============================================================ Mosaik views ==

sub base_actions {
    return (
        O(type => 'BackendRequestAction', url => "$API?action=home",    id => 'home',    postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=sz",        id => 'goSz',      postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=szpreview", id => 'szpreview', postValues => 'VALID'),
        O(type => 'ChangeSiteAction', id => 'clearSz',
          newContent => O(view => O(type => 'Box', id => 'szresult', children => [ button('Preview', 'szpreview') ]))),
        O(type => 'BackendRequestAction', url => "$API?action=form",    id => 'reform',  postValues => 'VALID'),
        O(type => 'BackendRequestAction', url => "$API?action=preview", id => 'preview', postValues => 'VALID'),
        O(type => 'BrowserAction', url => cfg('ROSEN_APP_URL', 'https://app.rosen.tech/events'), id => 'openRosen'),
        O(type => 'ChangeSiteAction', id => 'clearResult',
          newContent => O(view => O(type => 'Box', id => 'result', children => [ button('Preview', 'preview') ]))),
    );
}
sub mosaik_action { out_json(O(appVersion => N(APP_VERSION), action => $_[0])) }
sub mosaik_dialog { mosaik_action(O(type => 'DialogAction', id => 'msg', message => $_[0], positiveButtonText => 'OK')) }
sub mosaik_replace {
    my ($view, @extra) = @_;
    mosaik_action(O(type => 'ChangeSiteAction', id => 'show', newContent => O(actions => [ base_actions(), @extra ], view => $view)));
}
sub result_box {
    my ($rows) = @_;
    return O(type => 'Box', id => 'result',
             children => [ child(card(column($rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]);
}

sub form_view {
    my ($s) = @_;
    my %names = CHAINS;
    my @kids = (
        child(row([ child(label('Rosen bridge', 'HEADLINE2'), weight => N(1)), button('Change', 'home', style => 'TEXT') ]), align => 'JUSTIFY'),
        label('Send ERG and tokens from Ergo to another chain, over the Rosen bridge', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
    );
    if (!@{ $s->{chains} }) {
        push @kids, label('The bridge is unavailable right now.', 'BODY1', talign => 'CENTER');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    push @kids, child(row([
        O(type => 'Icon', icon => 'WARN', size => 'SMALL', tintColor => 'PRIMARY'),
        child(label('A transfer cannot be reversed. If the address on the other chain is wrong, the funds are lost.',
                    'BODY2BOLD', color => 'PRIMARY'), weight => N(1)),
    ], spacing => 'HALF_DEFAULT'), align => 'JUSTIFY');
    push @kids, child(card(column([
        child(label('Your Ergo wallet', 'BODY1BOLD'), align => 'START'),
        O(type => 'ErgoAddressChooseButton', id => 'address', value => $s->{address}, onValueChanged => 'reform',    # reloads: the balance shown belongs to it
          style => $s->{address} ? 'BUTTON_SECONDARY' : undef),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    # same order as the Rosen app: source, target, target address, token, amount
    my $t = $s->{token};
    my @form = (
        child(label('Source', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DropDownList', id => 'source', value => 'ergo', entries => O(ergo => 'Ergo')), align => 'JUSTIFY'),
        child(label('Target', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DropDownList', id => 'chain', value => $s->{chain}, onValueChanged => 'reform', placeholder => 'Select target chain',
                entries => O(map { $_ => $names{$_} } @{ $s->{chains} })), align => 'JUSTIFY'),
    );
    if ($s->{chain}) {
        my %hints = (cardano => 'starts with addr1', bitcoin => 'must start with bc1q', 'bitcoin-runes' => 'Taproot: must start with bc1p',
                     doge => 'starts with D', firo => 'starts with a', ethereum => 'starts with 0x', binance => 'starts with 0x');
        my $hint = $hints{ $s->{chain} };
        push @form,
            child(label('Target address', 'BODY1BOLD'), align => 'START'),
            child(O(type => 'TextInputField', id => 'to', value => $s->{to}, placeholder => "Your $names{ $s->{chain} } address ($hint)",
                    onValueChanged => 'clearResult', minValue => N(20), maxValue => N(120)), align => 'JUSTIFY'),
            child(label('Paste it; do not type it. Use an address of your own wallet on that chain, not an exchange deposit '
                        . 'address unless the exchange accepts this token on this chain.', 'BODY2', color => 'SECONDARY'), align => 'START'),
            child(label('Token', 'BODY1BOLD'), align => 'START'),
            child(O(type => 'DropDownList', id => 'token', value => $t ? $t->{id} : undef, onValueChanged => 'reform',
                    placeholder => 'Select token', entries => O(map { $_->{id} => $_->{name} } @{ $s->{list} })), align => 'JUSTIFY');
        push @form,
            child(label('Amount', 'BODY1BOLD'), align => 'START'),
            child(O(type => 'DecimalInputField', id => "amount_$t->{decimals}", value => defined $s->{amount} ? N($s->{amount}) : undef,
                    placeholder => "Amount of $t->{name}", onValueChanged => 'clearResult', minValue => N(1), scale => N($t->{decimals})), align => 'JUSTIFY')
            if $t;
        if ($t && $s->{address}) {    # what the address holds of the chosen token
            my $id = $t->{id} eq 'erg' ? '0' x 64 : $t->{id};
            my $b  = chain('balance', { address => $s->{address}, tokens => [$id] });
            push @form, child(label('You hold: ' . amt($b->{balances}{$id}, $t), 'BODY2', color => 'SECONDARY'), align => 'END')
                if $b->{ok} && ref $b->{balances} eq 'HASH' && ($b->{balances}{$id} // '') =~ /^\d+\z/;
        }
    }
    push @kids, child(card(column(\@form, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');
    push @kids, child(O(type => 'Box', id => 'result', children => [ button('Preview', 'preview') ]), align => 'JUSTIFY');
    push @kids, label('Only transfers out of Ergo. To bring funds to Ergo, start in the other chain\'s wallet at app.rosen.tech. '
                      . 'SigmaBridge only prepares the transaction; the Rosen bridge carries it out.', 'BODY2', color => 'SECONDARY', talign => 'CENTER');
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

# ================================================================= actions ==

sub act_app {
    out_json(O(
        manifest => O(
            appName => 'SigmaBridge', appDescription => 'Send ERG and tokens from Ergo to other chains over the Rosen bridge.',
            iconUrl => "$BASE/icon.png", appVersion => N(APP_VERSION), targetMosaikVersion => N(2),
            targetCanvasDimension => 'COMPACT_WIDTH', cacheLifetime => N(3600),
            notificationCheckUrl => "$API?action=notify",
        ),
        actions => [ base_actions() ],
        view    => home_view(undef),
    ));
}
sub act_home { mosaik_replace(home_view(good_address(posted_values()->{address}))) }
sub act_form { mosaik_replace(form_view(state(posted_values()))) }

# The start screen: the wallet address once, then the two ways out of Ergo, each explained.
sub home_view {
    my ($addr) = @_;
    my $note = sub { child(label($_[0], 'BODY2', color => 'SECONDARY'), align => 'START') };
    my @sz = (
        child(label('Swapuz', 'HEADLINE2'), align => 'START'),
        child(label('Sell ERG for another coin, such as BTC, ETH, USDT or XMR, paid to an address of yours on that coin\'s own chain.', 'BODY1'), align => 'START'),
        $note->('Swapuz is an exchange company, not a bridge. You send it ERG; it holds the ERG and pays out the other coin, usually within '
                . 'the hour. The rate is an estimate until it pays, it has a high minimum (several hundred ERG), and it can put an exchange '
                . 'on hold and ask for identity documents.'),
    );
    push @sz, sz_enabled() ? button('Swap with Swapuz', 'goSz') : $note->('Not available on this server right now.');
    return column([
        label('SigmaBridge', 'HEADLINE1', color => 'PRIMARY'),
        label('Take funds from Ergo to another chain', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
        child(card(column([
            child(label('Your Ergo wallet', 'BODY1BOLD'), align => 'START'),
            O(type => 'ErgoAddressChooseButton', id => 'address', value => $addr, onValueChanged => 'home',
              style => $addr ? 'BUTTON_SECONDARY' : undef),
            $addr ? () : $note->('Choose it once here. Both screens use it.'),
        ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY'),
        child(card(column([
            child(label('Rosen bridge', 'HEADLINE2'), align => 'START'),
            child(label('Move ERG or a token to Cardano, Ethereum, BNB Chain, Bitcoin, Dogecoin or Firo. It stays the same asset: '
                        . 'you receive it there in its wrapped form.', 'BODY1'), align => 'START'),
            $note->('A bridge run by a network of independent watchers and guards, with no company holding your funds and no '
                    . 'identity checks. Its fees are fixed amounts, so small transfers are expensive. Delivery takes an hour or two.'),
            button('Use the Rosen bridge', 'reform'),
        ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY'),
        child(card(column(\@sz, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY'),
        label('Both go out of Ergo only, and neither can be undone once sent. SigmaBridge prepares the transaction; '
              . 'the bridge or the exchange carries it out.', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
    ], id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub split_address {    # in groups of 6, so it can be compared by eye
    my ($a) = @_;
    return join(' ', unpack('(A6)*', $a));
}

sub act_preview {
    my $s = state(posted_values());
    my %names = CHAINS;
    return mosaik_dialog('Choose your Ergo wallet first.')            unless $s->{address};
    return mosaik_dialog('Choose the target chain.')                  unless $s->{chain};
    return mosaik_dialog("Enter your $names{ $s->{chain} } address.") unless defined $s->{to};
    return mosaik_dialog('Choose the token to send.')                 unless $s->{token};
    return mosaik_dialog('Enter the amount to send.')                 unless defined $s->{amount};

    my $t = $s->{token};
    my $q = chain('bridgequote', { token => $t->{id}, chain => $s->{chain}, amount => $s->{amount}, to => $s->{to}, address => $s->{address} }, 60);
    return mosaik_dialog($q->{error} || 'Could not get the bridge fees. Please try again.') unless $q->{ok};
    return mosaik_dialog('The smallest amount the bridge accepts for this right now is ' . amt($q->{min_transfer}, $t)
                         . ' (its fees are ' . amt(Math::BigInt->new($q->{bridge_fee})->badd($q->{network_fee})->bstr, $t) . ').')
        if Math::BigInt->new($s->{amount})->bcmp(Math::BigInt->new($q->{min_transfer})) < 0;

    my $fee   = cfg('MINER_FEE', 1100000) + 0;
    my $extra = $t->{id} eq 'erg' ? $fee + 1000000 : 0;
    my $short = Math::BigInt->new($s->{amount})->badd($extra)->bcmp(Math::BigInt->new($q->{balance} // '0')) > 0;
    my @rows = (
        kv_row('You send', amt($s->{amount}, $t)),
        kv_row('Bridge fee', amt($q->{bridge_fee}, $t)),
        kv_row("$names{ $s->{chain} } network fee", amt($q->{network_fee}, $t)),
        kv_row("Arrives on $names{ $s->{chain} }", amt($q->{receive}, $t), style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('Ergo network fee', fmt_amount($fee, 9) . ' ERG'),
        O(type => 'HorizontalRule', vPadding => 'HALF_DEFAULT'),
        child(label("To this $names{ $s->{chain} } address. Check every group:", 'BODY1BOLD'), align => 'START'),
        child(label(split_address($q->{to}), 'BODY1'), align => 'START'),
        child(label('It arrives as the wrapped or native form of this token on that chain. Rosen usually delivers within '
                    . 'an hour or two; it can take longer when the bridge is busy.', 'BODY2', color => 'SECONDARY'), align => 'START'),
    );
    my @extra;
    if ($short) {
        push @rows, label("Not enough $t->{name}: this address holds " . amt($q->{balance} // '0', $t) . '.', 'BODY1', talign => 'CENTER');
    }
    else {
        my $nonce = new_nonce();
        return mosaik_dialog('The bridge is unavailable right now. Please try again later.') unless defined $nonce && record_file($nonce);
        # what was previewed is stored; the wallet later asks for exactly this by its id
        save_record($nonce, { app => 'bridge', from_address => $s->{address}, t => time, draft => 1, token => $t->{id}, name => $t->{name}, decimals => $t->{decimals},
                              chain => $s->{chain}, to => $q->{to}, amount => $s->{amount},
                              max_fees => Math::BigInt->new($q->{bridge_fee})->badd($q->{network_fee})->bstr });
        watch_add('bridge', $nonce);
        push @extra,
            O(type => 'BackendRequestAction', url => "$API?action=status&n=$nonce", id => 'status', postValues => 'NONE'),
            O(type => 'ErgoPayAction', id => 'pay', onFinished => 'status',
              url => 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&n=$nonce&addr=$s->{address}");
        push @rows, button("Bridge to $names{ $s->{chain} }", 'pay');
    }
    push @rows, button('Refresh', 'preview', style => 'TEXT');
    mosaik_replace(result_box(\@rows), @extra);
}

sub act_status {
    my $nonce = $cgi->url_param('n');
    return mosaik_dialog('Unknown transfer.') unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/;
    my %names = CHAINS;
    my $self  = O(type => 'BackendRequestAction', url => "$API?action=status&n=$nonce", id => 'status', postValues => 'NONE');
    my $again = button('Refresh status', 'status', style => 'SECONDARY');
    my $back  = button('Back to the form', 'clearResult', style => 'TEXT');
    my $row   = sub { child(label($_[0], $_[1] || 'BODY1'), align => 'START') };
    my $rec   = load_record($nonce);
    my @rows;
    if (!$rec || !$rec->{tx_id}) {
        @rows = ($row->('Waiting for your wallet', 'BODY1BOLD'),
                 $row->('Your wallet has not picked this up yet. If you cancelled signing, nothing happened.'), $again, $back);
    }
    else {
        my $r  = chain('status', { tx_id => $rec->{tx_id}, watch_box => $rec->{watch_box}, mode => 'bridge', address => $rec->{address} });
        my $st = $r->{ok} ? ($r->{state} // 'unknown') : 'unknown';
        my $t  = { name => $rec->{name}, decimals => $rec->{decimals} };
        if ($st eq 'confirmed') {
            @rows = ($row->('On its way', 'BODY1BOLD'),
                     $row->(amt($rec->{amount}, $t) . " left Ergo. Rosen now delivers " . amt($rec->{receive}, $t)
                            . " to your $names{ $rec->{chain} } address, usually within an hour or two."),
                     button('Track it on Rosen', 'openRosen', style => 'SECONDARY'), $back);
        }
        elsif ($st eq 'pending') { @rows = ($row->('Submitted', 'BODY1BOLD'), $row->('Waiting for the next Ergo block (about two minutes).'), $again) }
        elsif ($st eq 'failed')  { @rows = ($row->('It did not go through', 'BODY1BOLD'),
                                            $row->('The funds it needed were spent by another transaction first. Nothing was sent. Preview again.'),
                                            button('Preview again', 'preview'), $back) }
        elsif (time - ($rec->{t_built} || time) < 180) {
            @rows = ($row->('Waiting for the network', 'BODY1BOLD'), $row->('Your transaction has not shown up yet. This can take a moment after signing.'), $again, $back);
        }
        else {
            @rows = ($row->('Not found on the network', 'BODY1BOLD'),
                     $row->("If you cancelled signing, nothing happened. Otherwise check your wallet's history before trying again."), $again, $back);
        }
    }
    mosaik_replace(result_box(\@rows), $self);
}

sub act_notify {
    my %names = CHAINS;
    my @sz = notifications('swapuz', \&sz_notify_check);
    my @ro = notifications('bridge', sub {
        my ($it) = @_;
        my $r = chain('status', { tx_id => $it->{tx_id}, watch_box => $it->{watch_box}, mode => 'bridge', address => $it->{address} });
        return (0) unless $r->{ok};
        my $st = $r->{state} // 'unknown';
        my $t  = { name => $it->{name}, decimals => $it->{decimals} };
        return (1, 'Your transfer of ' . amt($it->{amount}, $t) . " left Ergo and is on its way to $names{ $it->{chain} }.") if $st eq 'confirmed';
        return (1, 'Your bridge transfer did not go through. Nothing was sent.') if $st eq 'failed';
        return (1) if $st eq 'unknown' && time - ($it->{t} || 0) > 86400;
        return (0);
    });
    my @best = ($sz[1] || 0) > ($ro[1] || 0) ? @sz : @ro;    # the newer of the two messages
    notify_json($best[0], $best[1], $sz[2] < $ro[2] ? $sz[2] : $ro[2]);
}

# ================================================================= ErgoPay ==

sub ergopay_error { out_json(O(message => $_[0], messageSeverity => 'ERROR')) }

sub act_ergopay {
    my $nonce = $cgi->param('n');
    my $addr  = good_address(scalar $cgi->param('addr'));
    return ergopay_error('Invalid request.') unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/ && $addr;
    my $d = load_record($nonce) or return ergopay_error('This transfer request has expired. Start again in SigmaBridge.');
    return sz_ergopay($nonce, $addr, $d) if ($d->{app} // '') eq 'swapuz';
    return ergopay_error('This transfer request is not valid. Start again in SigmaBridge.') unless ($d->{app} // '') eq 'bridge';
    # only for the Ergo address the transfer was previewed with
    return ergopay_error('This transfer was prepared for a different wallet address. Start again in SigmaBridge.')
        unless ($d->{from_address} // '') eq $addr;
    if ($d->{tx_id}) {    # fetched before: fine to build again, unless that one is already on its way
        my $st = chain('status', { tx_id => $d->{tx_id}, watch_box => $d->{watch_box}, mode => 'bridge', address => $d->{address} });
        return ergopay_error('This transfer was already sent. Start a new one in SigmaBridge.')
            if $st->{ok} && ($st->{state} // '') =~ /^(pending|confirmed)\z/;
    }
    my %names = CHAINS;

    my $r = chain('bridgebuild', { map({ $_ => $d->{$_} } qw(token chain to amount max_fees)), address => $addr }, 60);
    return ergopay_error($r->{error} || 'The transfer could not be prepared. Please try again.') unless $r->{ok};
    save_record($nonce, { %$d, draft => 0, tx_id => $r->{tx_id}, watch_box => $r->{watch_box}, address => $r->{address},
                          receive => $r->{receive}, t_built => time });
    my $t = { name => $d->{name}, decimals => $d->{decimals} };
    warn "sigmabridge: built transfer tx $r->{tx_id} to $d->{chain}\n";    # audit trail, public data only
    out_json(O(
        reducedTx       => $r->{reduced_tx},
        address         => $r->{address},
        message         => 'SigmaBridge: send ' . amt($r->{amount}, $t) . " to $names{ $r->{chain} } over the Rosen bridge. "
                           . amt($r->{receive}, $t) . " arrives at $r->{to}. This cannot be reversed.",
        messageSeverity => 'WARNING',
    ));
}

# ================================================================== Swapuz ==
# The second way out: Swapuz (swapuz.com), a custodial instant exchange. ERG only.
# The order with Swapuz is opened when the wallet fetches the transaction, not at the
# preview, so looking at a rate never leaves an order behind. The helper checks the
# order Swapuz returns before any payment is built (see cmd_szorder in sigmaswap.py).

sub sz_enabled { return cfg('SWAPUZ_API_KEY') ? 1 : 0 }

sub sz_coins {    # Swapuz's payout coins, cached for ten minutes; a stale list beats none
    my $cd   = cache_dir();
    my $file = $cd ? File::Spec->catfile($cd, 'swapuz-coins.json') : undef;
    my $read = sub {
        open(my $fh, '<', $file) or return undef;
        local $/;
        my $d = eval { JSON::PP->new->utf8->decode(scalar <$fh>) };
        close $fh;
        return (ref $d eq 'HASH' && ref $d->{coins} eq 'ARRAY' && @{ $d->{coins} }) ? $d : undef;
    };
    if ($file && -f $file && time - (stat $file)[9] < 600) { my $d = $read->(); return $d if $d }
    my $r = chain('szcoins', {}, 70);
    if ($r->{ok} && ref $r->{coins} eq 'ARRAY' && @{ $r->{coins} }) {
        if ($file) {
            my $tmp = "$file.$$";
            if (open(my $fh, '>', $tmp)) { print {$fh} JSON::PP->new->utf8->encode($r); close $fh; rename($tmp, $file) or unlink $tmp }
        }
        return $r;
    }
    return ($file && -f $file) ? $read->() : undef;
}

sub sz_state {    # the Swapuz form's values, reduced to known-good ones
    my ($v, $list) = @_;
    my %s = (address => good_address($v->{address}));
    my $a = $v->{amount_9};
    $s{amount} = "$a" if defined $a && !ref $a && "$a" =~ /^[1-9]\d{0,18}\z/;
    ($s{coin}) = grep { defined $v->{szcoin} && !ref $v->{szcoin} && $_->{c} eq $v->{szcoin} } @{ $list->{coins} };
    if ($s{coin}) {
        my @nets = @{ $s{coin}{nets} };
        ($s{net}) = grep { defined $v->{sznet} && !ref $v->{sznet} && $_->{s} eq $v->{sznet} } @nets;
        ($s{net}) = grep { $_->{def} } @nets unless $s{net};      # their default network, else the only/first one
        $s{net} ||= $nets[0];
    }
    for my $k (qw(szto szmemo)) {
        my $t = $v->{$k};
        next unless defined $t && !ref $t;
        $t =~ s/\s+//g;
        $s{$k} = $t if $t =~ /^[A-Za-z0-9:._\-]{1,130}\z/;
    }
    return \%s;
}

sub sz_matches {    # does $text fit Swapuz's pattern for this network? (their pattern, so bounded and guarded)
    my ($text, $rx) = @_;
    return 1 unless defined $rx && length $rx;
    my $ok = 1;
    eval {
        local $SIG{ALRM} = sub { die "slow\n" };
        alarm 2;
        my $re = qr/$rx/;
        $ok = $text =~ $re ? 1 : 0;
        alarm 0;
        1;
    } or do { alarm 0; $ok = 1 };    # an unusable pattern is no reason to refuse; Swapuz checks the address too
    return $ok;
}

sub sz_view {
    my ($s, $list) = @_;
    my @kids = (
        child(row([ child(label('Swapuz', 'HEADLINE2'), weight => N(1)), button('Change', 'home', style => 'TEXT') ]), align => 'JUSTIFY'),
        label('Sell ERG for another coin through the Swapuz exchange', 'BODY2', color => 'SECONDARY', talign => 'CENTER'),
    );
    if (!$list || !$list->{erg_ok}) {
        push @kids, label($list ? 'Swapuz is not taking ERG right now. Please try again later.'
                                : 'Swapuz is not answering right now. Please try again later.', 'BODY1', talign => 'CENTER');
        return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
    }
    push @kids, child(row([
        O(type => 'Icon', icon => 'WARN', size => 'SMALL', tintColor => 'PRIMARY'),
        child(label('Swapuz is a company that holds your ERG until it pays out. An exchange cannot be reversed, and a wrong '
                    . 'receiving address loses the funds.', 'BODY2BOLD', color => 'PRIMARY'), weight => N(1)),
    ], spacing => 'HALF_DEFAULT'), align => 'JUSTIFY');
    push @kids, child(card(column([
        child(label('Your Ergo wallet', 'BODY1BOLD'), align => 'START'),
        O(type => 'ErgoAddressChooseButton', id => 'address', value => $s->{address}, onValueChanged => 'goSz',
          style => $s->{address} ? 'BUTTON_SECONDARY' : undef),
    ], padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');

    my @form = (
        child(label('You send', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DecimalInputField', id => 'amount_9', value => defined $s->{amount} ? N($s->{amount}) : undef,
                placeholder => 'Amount of ERG', onValueChanged => 'clearSz', minValue => N(1), scale => N(9)), align => 'JUSTIFY'),
    );
    if ($s->{address}) {
        my $zero = '0' x 64;
        my $b = chain('balance', { address => $s->{address}, tokens => [$zero] });
        push @form, child(label('You hold: ' . fmt_amount($b->{balances}{$zero}, 9) . ' ERG', 'BODY2', color => 'SECONDARY'), align => 'END')
            if $b->{ok} && ref $b->{balances} eq 'HASH' && ($b->{balances}{$zero} // '') =~ /^\d+\z/;
    }
    push @form,
        child(label('You receive', 'BODY1BOLD'), align => 'START'),
        child(O(type => 'DropDownList', id => 'szcoin', value => $s->{coin} ? $s->{coin}{c} : undef, onValueChanged => 'goSz',
                placeholder => 'Select coin', entries => O(map { $_->{c} => "$_->{c}  \x{00B7}  $_->{n}" } @{ $list->{coins} })), align => 'JUSTIFY');
    if (my $c = $s->{coin}) {
        my $n = $s->{net};
        push @form,
            child(label('On this network', 'BODY1BOLD'), align => 'START'),
            child(O(type => 'DropDownList', id => 'sznet', value => $n->{s}, onValueChanged => 'goSz',
                    entries => O(map { $_->{s} => $_->{n} } @{ $c->{nets} })), align => 'JUSTIFY'),
            child(label('The same coin exists on several networks. Choose the one your receiving wallet or exchange account '
                        . 'really uses: sent to the wrong network, it is lost.', 'BODY2', color => 'SECONDARY'), align => 'START')
            if @{ $c->{nets} } > 1;
        push @form,
            child(label("Your $c->{c} address", 'BODY1BOLD'), align => 'START'),
            child(O(type => 'TextInputField', id => 'szto', value => $s->{szto}, placeholder => "Where the $c->{c} is paid ($n->{n})",
                    onValueChanged => 'clearSz', minValue => N(10), maxValue => N(130)), align => 'JUSTIFY'),
            child(label('Paste it; do not type it.', 'BODY2', color => 'SECONDARY'), align => 'START');
        push @form,
            child(label('Memo / tag', 'BODY1BOLD'), align => 'START'),
            child(O(type => 'TextInputField', id => 'szmemo', value => $s->{szmemo}, placeholder => 'Required by some exchanges for this coin',
                    onValueChanged => 'clearSz', maxValue => N(120)), align => 'JUSTIFY'),
            child(label('If the receiving account needs a memo or tag and it is missing, the payout is lost.', 'BODY2', color => 'SECONDARY'), align => 'START')
            if $n->{memo};
    }
    push @kids, child(card(column(\@form, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), align => 'JUSTIFY');
    push @kids, child(O(type => 'Box', id => 'szresult', children => [ button('Preview', 'szpreview') ]), align => 'JUSTIFY');
    push @kids, label('Exchange services are provided by Swapuz. By swapping you accept the Swapuz Terms of Use and its AML/KYC '
                      . 'policy. SigmaBridge only prepares the payment.', 'BODY2', color => 'SECONDARY', talign => 'CENTER');
    return column(\@kids, id => 'main', padding => 'DEFAULT', spacing => 'DEFAULT');
}

sub act_sz {
    return mosaik_dialog('Swapuz is not available on this server right now.') unless sz_enabled();
    my $list = sz_coins();
    mosaik_replace(sz_view(sz_state(posted_values(), $list || { coins => [] }), $list));
}

sub sz_box {
    my ($rows) = @_;
    return O(type => 'Box', id => 'szresult',
             children => [ child(card(column($rows, padding => 'DEFAULT', spacing => 'HALF_DEFAULT')), hAlign => 'JUSTIFY') ]);
}

sub act_szpreview {
    return mosaik_dialog('Swapuz is not available on this server right now.') unless sz_enabled();
    my $list = sz_coins() or return mosaik_dialog('Swapuz is not answering right now. Please try again.');
    my $s = sz_state(posted_values(), $list);
    return mosaik_dialog('Choose your Ergo wallet first.')   unless $s->{address};
    return mosaik_dialog('Enter how much ERG to send.')      unless defined $s->{amount};
    return mosaik_dialog('Choose the coin to receive.')      unless $s->{coin};
    my ($c, $n) = ($s->{coin}, $s->{net});
    return mosaik_dialog("Enter your $c->{c} address.") unless defined $s->{szto} && length $s->{szto} >= 10;
    return mosaik_dialog("That does not look like a $c->{c} address on $n->{n}. Check the address and the network.")
        unless sz_matches($s->{szto}, $n->{rx});
    my $memo = $n->{memo} ? $s->{szmemo} : undef;
    return mosaik_dialog('That memo does not have the form this network uses.') if defined $memo && !sz_matches($memo, $n->{mrx});

    my $q = chain('szquote', { to => $c->{c}, net => $n->{s}, amount => $s->{amount}, address => $s->{address} }, 60);
    return mosaik_dialog($q->{error} || 'Swapuz is not answering right now. Please try again.') unless $q->{ok};
    return mosaik_dialog('Swapuz is not answering right now. Please try again.') unless ($q->{out} // '') =~ /^\d+(\.\d+)?\z/;

    my $fee   = cfg('MINER_FEE', 1100000) + 0;
    my $short = Math::BigInt->new($s->{amount})->badd($fee + 1000000)->bcmp(Math::BigInt->new(($q->{balance} // '') =~ /^\d+\z/ ? $q->{balance} : 0)) > 0;
    # the order is refused if Swapuz's estimate has fallen more than 3 % by the time of signing
    my $floor = Math::BigFloat->new($q->{out})->bmul('0.97')->bfround(-12)->bstr;
    my @rows = (
        kv_row('You send', fmt_amount($s->{amount}, 9) . ' ERG'),
        kv_row('You receive (estimate)', "\x{2248} $q->{out} $c->{c}", style => 'HEADLINE2', color => 'PRIMARY'),
        kv_row('Network', $n->{n}),
        kv_row("Swapuz's payout fee", "$q->{withdraw_fee} $c->{c}"),
        kv_row('Ergo network fee', fmt_amount($fee, 9) . ' ERG'),
        O(type => 'HorizontalRule', vPadding => 'HALF_DEFAULT'),
        child(label("Paid to this $c->{c} address. Check every group:", 'BODY1BOLD'), align => 'START'),
        child(label(split_address($s->{szto}), 'BODY1'), align => 'START'),
        (defined $memo ? child(label("Memo / tag: $memo", 'BODY1'), align => 'START') : ()),
        child(label('The rate floats: the amount is fixed only when Swapuz receives your ERG, and its payout fee may come out of '
                    . 'it. Swapuz holds the ERG in between and can put an exchange on hold for identity checks. If it cannot '
                    . 'complete, it refunds to your Ergo address.', 'BODY2', color => 'SECONDARY'), align => 'START'),
    );
    my @extra;
    if ($short) {
        push @rows, label('Not enough ERG: this address holds ' . fmt_amount($q->{balance} // '0', 9) . ' ERG, and the network fee comes on top.',
                          'BODY1', talign => 'CENTER');
    }
    else {
        my $nonce = new_nonce();
        return mosaik_dialog('Swapuz is unavailable right now. Please try again later.') unless defined $nonce && record_file($nonce);
        save_record($nonce, { app => 'swapuz', from_address => $s->{address}, t => time, draft => 1, to => $c->{c}, net => $n->{s},
                              net_name => $n->{n}, dest => $s->{szto}, memo => $memo, amount => $s->{amount}, est => $q->{out}, min_out => $floor });
        watch_add('swapuz', $nonce);
        push @extra,
            O(type => 'BackendRequestAction', url => "$API?action=szstatus&n=$nonce", id => 'szstatus', postValues => 'NONE'),
            O(type => 'ErgoPayAction', id => 'szpay', onFinished => 'szstatus',
              url => 'ergopay://' . ergopay_host() . "/api.pl?action=ergopay&n=$nonce&addr=$s->{address}");
        push @rows, button("Swap for $c->{c} with Swapuz", 'szpay');
    }
    push @rows, button('Refresh', 'szpreview', style => 'TEXT');
    mosaik_replace(sz_box(\@rows), @extra);
}

sub sz_ergopay {
    my ($nonce, $addr, $d) = @_;
    return ergopay_error('Swapuz is not available on this server right now.') unless sz_enabled();
    return ergopay_error('This exchange was prepared for a different wallet address. Start again in SigmaBridge.')
        unless ($d->{from_address} // '') eq $addr;
    return ergopay_error('This exchange request has expired. Preview it again in SigmaBridge.') if time - ($d->{t} || 0) > 1800;
    if ($d->{tx_id}) {    # fetched before: fine to build again, unless that payment is already on its way
        my $st = chain('status', { tx_id => $d->{tx_id}, watch_box => $d->{watch_box}, mode => 'bridge', address => $d->{address} });
        return ergopay_error('This exchange was already paid. Start a new one in SigmaBridge.')
            if $st->{ok} && ($st->{state} // '') =~ /^(pending|confirmed)\z/;
    }
    my $r = chain('szorder', { map({ $_ => $d->{$_} } qw(to net dest memo amount min_out)), address => $addr, uuid => $nonce,
                               ($d->{uid} ? (order => { uid => $d->{uid}, deposit => $d->{deposit} }) : ()) }, 90);
    return ergopay_error($r->{error} || 'The exchange could not be prepared. Please try again.') unless $r->{ok};
    save_record($nonce, { %$d, draft => 0, tx_id => $r->{tx_id}, watch_box => $r->{watch_box}, address => $r->{address},
                          uid => $r->{uid}, deposit => $r->{deposit}, out => $r->{out}, t_built => time });
    warn "sigmabridge: swapuz order $r->{uid} to $d->{to}, payment tx $r->{tx_id}\n";    # audit trail: no addresses, no amounts
    out_json(O(
        reducedTx       => $r->{reduced_tx},
        address         => $r->{address},
        message         => 'SigmaBridge via Swapuz: send ' . fmt_amount($r->{amount}, 9) . " ERG to Swapuz (order $r->{uid}, deposit address "
                           . "$r->{deposit}) for about $r->{out} $d->{to} on $d->{net_name}, paid to $d->{dest}"
                           . (defined $d->{memo} ? " with memo $d->{memo}" : '') . '. Swapuz is a custodial exchange: it holds the ERG '
                           . 'until it pays out, the rate is an estimate, and it may hold an exchange for identity checks. '
                           . ($r->{refund_set} ? 'Refunds go to your Ergo address. ' : '') . 'This cannot be reversed. Network fee '
                           . fmt_amount($r->{miner_fee}, 9) . ' ERG.',
        messageSeverity => 'WARNING',
    ));
}

sub act_szstatus {
    my $nonce = $cgi->url_param('n');
    return mosaik_dialog('Unknown exchange.') unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/;
    my $self  = O(type => 'BackendRequestAction', url => "$API?action=szstatus&n=$nonce", id => 'szstatus', postValues => 'NONE');
    my $again = button('Refresh status', 'szstatus', style => 'SECONDARY');
    my $back  = button('Back to the form', 'clearSz', style => 'TEXT');
    my $row   = sub { child(label($_[0], $_[1] || 'BODY1'), align => 'START') };
    my $rec   = load_record($nonce);
    my (@rows, @extra);
    if (!$rec || ($rec->{app} // '') ne 'swapuz' || !$rec->{tx_id} || !$rec->{uid}) {
        @rows = ($row->('Waiting for your wallet', 'BODY1BOLD'),
                 $row->('Your wallet has not picked this up yet. If you cancelled signing, nothing was sent.'), $again, $back);
        return mosaik_replace(sz_box(\@rows), $self);
    }
    my $pay = chain('status', { tx_id => $rec->{tx_id}, watch_box => $rec->{watch_box}, mode => 'bridge', address => $rec->{address} });
    my $ps  = $pay->{ok} ? ($pay->{state} // 'unknown') : 'unknown';
    my $o   = chain('szstatus', { uid => $rec->{uid}, deposit => $rec->{deposit} });
    my $os  = $o->{ok} ? $o->{status} : -1;
    my $sent = fmt_amount($rec->{amount}, 9) . ' ERG';
    my $help = "If something looks wrong, write to support\@swapuz.com with order id $rec->{uid}.";
    push @extra, O(type => 'CopyClipboardAction', text => $rec->{uid}, id => 'copyOrder');
    my $copy = button("Copy order id $rec->{uid}", 'copyOrder', style => 'TEXT');

    if ($o->{ok} && $o->{aml}) {
        @rows = ($row->('On hold at Swapuz', 'BODY1BOLD'),
                 $row->("Swapuz has put this exchange on hold for a check. Write to support\@swapuz.com with order id $rec->{uid}; "
                        . 'they will tell you what they need. SigmaBridge cannot release it.'), $copy, $again);
    }
    elsif ($os == 6) {
        @rows = ($row->('Exchange complete', 'BODY1BOLD'),
                 $row->("Swapuz paid $o->{out} $rec->{to} for your $sent. It can take a few more minutes to show in the receiving wallet."), $copy, $back);
    }
    elsif ($os >= 1 && $os <= 5) {
        @rows = ($row->('Swapuz is exchanging', 'BODY1BOLD'),
                 $row->("Swapuz received your $sent and is paying out about $o->{out} $rec->{to}. This usually takes under an hour."), $copy, $again);
    }
    elsif ($os == 10 && $ps !~ /^(pending|confirmed)\z/) {
        @rows = ($row->('Expired', 'BODY1BOLD'), $row->('The payment was not sent in time and Swapuz closed the order. Nothing was sent. Preview again.'),
                 button('Preview again', 'szpreview'), $back);
    }
    elsif ($os == 10) {
        @rows = ($row->('Overdue at Swapuz', 'BODY1BOLD'),
                 $row->("Your $sent left your wallet, but Swapuz marked the order overdue. $help"), $copy, $again);
    }
    elsif ($ps eq 'confirmed') {
        @rows = ($row->('Payment sent', 'BODY1BOLD'),
                 $row->("Your $sent is on the Ergo blockchain. Swapuz starts once it has seen enough confirmations, usually within 20 minutes."), $copy, $again);
    }
    elsif ($ps eq 'pending') {
        @rows = ($row->('Payment submitted', 'BODY1BOLD'), $row->('Waiting for the next Ergo block (about two minutes).'), $copy, $again);
    }
    elsif ($ps eq 'failed') {
        @rows = ($row->('It did not go through', 'BODY1BOLD'),
                 $row->('The funds it needed were spent by another transaction first. Nothing was sent. Preview again.'),
                 button('Preview again', 'szpreview'), $back);
    }
    elsif (time - ($rec->{t_built} || time) < 180) {
        @rows = ($row->('Waiting for the network', 'BODY1BOLD'), $row->('Your payment has not shown up yet. This can take a moment after signing.'), $again, $back);
    }
    else {
        @rows = ($row->('Not found on the network', 'BODY1BOLD'),
                 $row->("If you cancelled signing, nothing was sent. Otherwise check your wallet's history before trying again."), $again, $back);
    }
    mosaik_replace(sz_box(\@rows), $self, @extra);
}

sub sz_notify_check {    # for notifications(): (is_final, message)
    my ($it) = @_;
    return (1) unless $it->{uid} && $it->{deposit};
    my $o = chain('szstatus', { uid => $it->{uid}, deposit => $it->{deposit} });
    if ($o->{ok}) {
        return (1, "Swapuz put your exchange on hold. Write to support\@swapuz.com with order id $it->{uid}.") if $o->{aml};
        return (1, 'Your Swapuz exchange is complete: ' . "$o->{out} $it->{to} was paid for " . fmt_amount($it->{amount}, 9) . ' ERG.') if $o->{status} == 6;
        return (1) if $o->{status} == 10;
    }
    return (1) if time - ($it->{t} || 0) > 2 * 86400;
    return (0);
}
