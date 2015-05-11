
from bayeslite.sqlite3_util import sqlite3_quote_name as quote

from cStringIO import StringIO
import string
import os

PLOTTING_COMMANDS = ['.heatmap', '.histogram', '.show']


def is_dot_command(line):
    if is_blank_line(line):
        return False
    return line[0] == '.'


def is_continuation(line):
    if is_blank_line(line):
        return False
    return line[0] in string.whitespace


def is_plotting_command(line):
    if not is_dot_command(line):
        return False
    return line.split()[0] in PLOTTING_COMMANDS


def is_blank_line(line):
    return len(line) == 0 or line.isspace()


def is_comment(line):
    if len(line) < 2:
        return False
    return line[:2] == '--'


def get_line_type(line):
    if is_comment(line):
        return 'comment'
    elif is_blank_line(line):
        return 'blank'
    else:
        return 'code'


def do_and_cap(shell, cmd):
    assert len(cmd) > 0

    backup = shell.stdout
    stream = StringIO()
    shell.stdout = stream

    shell.onecmd(cmd)

    shell.stdout = backup
    output = stream.getvalue()
    stream.close()

    return output


def clean_cmd_filename(cmd, fignum, output_dir):
    if ' -f ' not in cmd and ' --filename ' not in cmd:
        figname = 'fig_' + str(fignum) + '.png'
        filename = os.path.join(output_dir, figname)
        cmd += ' --filename ' + filename
    else:
        raise ValueError
    return cmd, figname


def exec_and_cap_cmd(cmd, fignum, shell, mdstr, output_dir):
    plotting_cmd = is_plotting_command(cmd)
    if plotting_cmd:
        cmd, figfile = clean_cmd_filename(cmd, fignum, output_dir)
        fignum += 1
    # do the comand and grab the output, and close the code markup
    output = do_and_cap(shell, cmd)
    output = '\n'.join(['    ' + s for s in output.split('\n')])
    mdstr += '\n' + output
    if plotting_cmd:
        mdstr += "\n![{}]({})\n".format(cmd, figfile)
    cmd = ''
    return cmd, mdstr, fignum


def mdread(f, output_dir, shell):
    """Reads a .bql file and converts it to markdown.

    Captures text and figure output and saves all code and image assets to
    a directory. Using `mdread` requires some special care on behalf of the
    user. See `writing a BQL script`.

    Parameters
    ----------
    f : file
        The .bql file to convert.
    output_dir : string
        The name of an output directory where all assets will be saved.
    shell : bayeslite.Shell
        The shell object from which the funciton was called.

    Returns
    -------
    mdstr : string
        A string of markdown

    Notes
    -----
    When writing a script for mdread, do not use ``--filename`` arguments for
    plotting commands. `mdread` will generate filenames that ensure assets are
    saves alongside the markdown.
    """
    if not isinstance(f, file):
        raise TypeError('f should be a file.')
    lines = f.read().split('\n')

    cont = False
    cmd = ''
    mdstr = ''
    last_type = None
    fignum = 0

    # XXX: The first three chracters are stripped from comments, because it is
    # assumed that a space immediately follows '--'. I should probably change
    # this is future.
    line = lines[0]
    last_type = get_line_type(line)
    if last_type == 'code':
        mdstr += "\n"
        cmd = line.strip()
        mdstr += '    ' + line
    elif last_type == 'comment':
        mdstr += line[3:].rstrip() + '\n'
    else:
        mdstr += line

    for i, line in enumerate(lines[1:]):
        linetype = get_line_type(line)
        cont = is_continuation(line)
        if cont and linetype != last_type:
            raise ValueError
        if cont:
            if linetype == 'code':
                cmd += ' ' + line.strip()
                mdstr += '\n' + '    ' + line
        else:
            if last_type == 'code':
                cmd, mdstr, fignum = exec_and_cap_cmd(cmd, fignum, shell,
                                                      mdstr, output_dir)
            if linetype == 'comment':
                mdstr += line[3:].rstrip() + '\n'
            elif linetype == 'code':
                cmd = line
                mdstr += '\n    ' + line
                if i == len(lines)-1:
                    last_cmd, mdstr, fignum = exec_and_cap_cmd(
                        cmd, fignum, shell, mdstr, output_dir)
            else:
                mdstr += '\n'
        last_type = linetype

    if last_type == 'code' and cont:
        last_cmd, mdstr, fignum = exec_and_cap_cmd(cmd, fignum, shell, mdstr,
                                                   output_dir)

    with open(os.path.join(output_dir, 'README.md'), 'w') as f:
        f.write(mdstr)

    return mdstr


def nullify(bdb, table, value):
    """Relace specified values in a SQL table with ``NULL``

    Parameters
    ----------
    bdb : bayeslite.BayesDB
        bayesdb database object
    table : str
        The name of the table on which to act
    value : stringable
        The value to replace with ``NULL``

    Examples
    --------
    >>> import bayeslite
    >>> from bdbcontrib import plotutils
    >>> with bayeslite.bayesdb_open('mydb.bdb') as bdb:
    >>>    utils.nullifty(bdb, 'mytable', 'NaN')

    """
    # get a list of columns of the table
    c = bdb.sql_execute('pragma table_info({})'.format(quote(table)))
    columns = [r[1] for r in c.fetchall()]
    for col in columns:
        bql = '''
        UPDATE {} SET {} = NULL WHERE {} = ?;
        '''.format(quote(table), quote(col), quote(col))
        bdb.sql_execute(bql, (value,))


def unicorn():
    """It's not a unicorn at all!"""
    unicorn = """
                                 ,-""   `.
                               ,'  _   e )`-._
                              /  ,' `-._<.===-'
                             /  /
                            /  ;
                _          /   ;
   (`._    _.-"" ""--..__,'    |
   <_  `-""                     \\
    <`-                          :
     (__   <__.                  ;
       `-.   '-.__.      _.'    /
          \\      `-.__,-'    _,'
           `._    ,    /__,-'
              ""._\\__,'< <____
                   | |  `----.`.
                   | |        \\ `.
                   ; |___      \\-``
                   \\   --<
                    `.`.<
               hjw    `-'
    """
    return unicorn