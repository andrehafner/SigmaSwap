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
use AppConfig qw(cfg);

our @EXPORT = qw(O N TRUE FALSE enc enc_str out_json die_json success_json fmt_amount cache_dir label child column row card button kv_row new_nonce record_file save_record load_record);

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

1;
