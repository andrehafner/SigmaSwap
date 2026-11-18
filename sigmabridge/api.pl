#!/usr/bin/perl
# SigmaBridge - send ERG and tokens from Ergo to another chain over the Rosen bridge.
# A subdirectory app of SigmaSwap (shared config, helper, venv, cache):
#   https://<host>/apps/sigmaswap/sigmabridge/
#
#   Mosaik
#     GET  api.pl                  -> the form
#     POST api.pl?action=form      -> re-render (token / chain changed)
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
use File::Spec;
use AppConfig qw(cfg base_url chain);
use MosaikKit;

use constant APP_VERSION => 3;
use constant CHAINS      => (cardano => 'Cardano', ethereum => 'Ethereum', binance => 'BNB Chain', 'bitcoin-runes' => 'Bitcoin Runes',
                            bitcoin => 'Bitcoin', doge => 'Dogecoin', firo => 'Firo');
use constant CHAIN_ORDER => qw(cardano ethereum binance bitcoin-runes bitcoin doge firo);    # same order as the Rosen app

$CGI::POST_MAX        = 64 * 1024;
$CGI::DISABLE_UPLOADS = 1;

my $cgi = CGI->new;
print $cgi->header(-type => 'application/json', -charset => 'utf-8', -cache_control => 'no-store');

my $BASE = base_url();
my $API  = "$BASE/api.pl";

my $action = $cgi->url_param('action');
$action = $cgi->param('action') unless defined $action && $action ne '';
$action = '' unless defined $action;

my %mosaik = ('' => \&act_app, form => \&act_form, preview => \&act_preview, status => \&act_status);

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
        label('SigmaBridge', 'HEADLINE1', color => 'PRIMARY'),
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
        O(type => 'ErgoAddressChooseButton', id => 'address', value => $s->{address}, onValueChanged => 'clearResult',
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
        view    => form_view(state({})),
    ));
}
sub act_form { mosaik_replace(form_view(state(posted_values()))) }

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
        save_record($nonce, { t => time, draft => 1, token => $t->{id}, name => $t->{name}, decimals => $t->{decimals},
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
    notify_json(notifications('bridge', sub {
        my ($it) = @_;
        my $r = chain('status', { tx_id => $it->{tx_id}, watch_box => $it->{watch_box}, mode => 'bridge', address => $it->{address} });
        return (0) unless $r->{ok};
        my $st = $r->{state} // 'unknown';
        my $t  = { name => $it->{name}, decimals => $it->{decimals} };
        return (1, 'Your transfer of ' . amt($it->{amount}, $t) . " left Ergo and is on its way to $names{ $it->{chain} }.") if $st eq 'confirmed';
        return (1, 'Your bridge transfer did not go through. Nothing was sent.') if $st eq 'failed';
        return (1) if $st eq 'unknown' && time - ($it->{t} || 0) > 86400;
        return (0);
    }));
}

# ================================================================= ErgoPay ==

sub ergopay_error { out_json(O(message => $_[0], messageSeverity => 'ERROR')) }

sub act_ergopay {
    my $nonce = $cgi->param('n');
    my $addr  = good_address(scalar $cgi->param('addr'));
    return ergopay_error('Invalid request.') unless defined $nonce && $nonce =~ /^[0-9a-f]{32}\z/ && $addr;
    my $d = load_record($nonce) or return ergopay_error('This transfer request has expired. Start again in SigmaBridge.');
    return ergopay_error('This transfer was already prepared. Start a new one in SigmaBridge.') if $d->{tx_id};
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
